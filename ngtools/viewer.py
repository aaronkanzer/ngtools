import sys
import re
import json
import os.path
import argparse
import textwrap
import shlex                    # parse user input as if a shell commandline
import readline                 # autocomplete/history in user input
import atexit                   # do stuff whe exiting (save history...)
import numpy as np
import neuroglancer as ng
from urllib.parse import urlparse, unquote as urlunquote, quote as urlquote
from neuroglancer.server import global_server_args
from .fileserver import LocalFileServerInBackground
from .volume import LocalSource, RemoteSource
from .spaces import (
    neurotransforms, letter2full, compose, to_square)
from .shaders import shaders, colormaps, pretty_colormap_list
from .transforms import load_affine
from .opener import remote_protocols
from .utils import bcolors


_print = print


def action(func):
    """
    Decorator for neuroglancer actions (that can be triggered  by argparse)
    """
    def wrapper(self, *args, **kwargs):
        args = list(args)
        if args and isinstance(args[0], argparse.Namespace):
            if len(args) > 1:
                raise ValueError('Only one positional argument accepted '
                                 'when an action is applied to an argparse '
                                 'object')
            parsedargs = vars(args.pop(0))
            parsedargs.update(kwargs)
            parsedargs.pop('func', None)
            kwargs = parsedargs
        return func(self, *args, **kwargs)

    return wrapper


def ensure_list(x):
    if isinstance(x, np.ndarray):
        x = x.tolist()
    if not isinstance(x, (list, tuple)):
        x = [x]
    return list(x)


class LocalNeuroglancer:
    """
    A local instance of neuroglancer that can launch its own local fileserver.

    It also comes with a shell-like interfact that allows loading,
    unloading, applying transforms, etc.
    """

    def __init__(self, port=9321, token=1, fileserver=True):
        """
        Parameters
        ----------
        port : int
            Port to use
        token : str
            Unique id for the instance
        filserver : LocalFileServerInBackground or bool
            A local file server.

            * If `True`, create a local file server.
            * If `False`, do not create one -- only remote files can be loaded.
        """
        if fileserver is True:
            fileserver = LocalFileServerInBackground()
        self.fileserver = fileserver
        if self.fileserver:
            self.fileserver.start_and_serve_forever()
        self.port = port
        global_server_args['bind_port'] = str(port)
        self.viewer = ng.Viewer(token=str(token))
        self.parser = self.make_parser()
        self.comp = UserInputCompleter([
            # Any chance we can get these from self.parser?
            'help',
            'load',
            'unload',
            'transform',
            'shader',
            'display',
            'exit',
        ])
        self.display_dimensions = ['x', 'y', 'z']

    def await_input(self):
        print(
            f'\nType {bcolors.bold}help{bcolors.endc} to list available '
            f'commands, or {bcolors.bold}help <command>{bcolors.endc} '
            f'for specific help.'
        )
        count = 1
        try:
            while True:
                # Query input
                args = input(f'{bcolors.fg.green}[{count}] {bcolors.endc}')
                if not args.strip():
                    continue
                count += 1

                # Parse
                try:
                    args = self.parser.parse_args(shlex.split(args))
                    if not vars(args):
                        raise ValueError("Unknown command")
                except KeyboardInterrupt as e:
                    raise e
                except Exception as e:
                    print(f"{bcolors.fail}(PARSE ERROR)", e, bcolors.endc,
                          file=sys.stderr)
                    continue

                # Execute
                try:
                    if hasattr(args, 'func'):
                        args.func(args)
                    self.display()
                except KeyboardInterrupt as e:
                    raise e
                except Exception as e:
                    print(f"{bcolors.fail}(EXEC ERROR)", e, bcolors.endc,
                          file=sys.stderr)
                    raise e
                    continue

        except KeyboardInterrupt:
            print('exit')

    def make_parser(self):
        mainparser = NoExitArgParse('')
        parsers = mainparser.add_subparsers(required=True)

        # ==============================================================
        #   HELP
        # ==============================================================
        help = parsers.add_parser('help', help='Display help')
        help.set_defaults(func=self.help)
        help.add_argument(
            dest='action', nargs='?', help='Command for which to display help')

        # ==============================================================
        #   LOAD
        # ==============================================================
        load = parsers.add_parser('load', help='Load a file')
        load.set_defaults(func=self.load)
        load.add_argument(
            dest='filename', nargs='+', help='Filename(s) with protocols')
        load.add_argument(
            '--name', help='A name for the image layer')
        load.add_argument(
            '--transform', help='Apply a transform')

        # ==============================================================
        #   UNLOAD
        # ==============================================================
        unload = parsers.add_parser('unload', help='Unload a file')
        unload.set_defaults(func=self.unload)
        unload.add_argument(
            dest='layer', nargs='+', help='Layer(s) to unload')

        # ==============================================================
        #   TRANSFORM
        # ==============================================================
        transform = parsers.add_parser('transform', help='Apply a transform')
        transform.set_defaults(func=self.transform)
        transform.add_argument(
            dest='transform', nargs='+',
            help='Path to transform file or flattened transformation '
                 'matrix (row major)')
        transform.add_argument(
            '--layer', nargs='+', help='Layer(s) to transform')
        transform.add_argument(
            '--inv', action='store_true', default=False,
            help='Invert the transform before applying it')
        transform.add_argument(
            '--mov', help='Moving image (required by some formats)')
        transform.add_argument(
            '--fix', help='Fixed image (required by some formats)')

        # ==============================================================
        #   SHADER
        # ==============================================================
        description = textwrap.dedent(
            """
            Applies a colormap, or a more advanced shading function to all
            or some of the layers.

            List of builtin colormaps
            -------------------------
            """
        ) + textwrap.indent(pretty_colormap_list(), ' ')
        shader = parsers.add_parser(
            'shader', help='Apply a shader', description=description,
            formatter_class=argparse.RawDescriptionHelpFormatter)
        shader.set_defaults(func=self.shader)
        shader.add_argument(
            dest='shader', help='Shader name or GLSL shader code')
        shader.add_argument(
            '--layer', nargs='+', help='Layer(s) to apply shader to')

        # ==============================================================
        #   DISPLAY
        # ==============================================================
        display = parsers.add_parser('display', help='Dimensions to display')
        display.set_defaults(func=self.display)
        display.add_argument(
            dest='dimension', nargs='*', help='Dimensions to display')

        # ==============================================================
        #   LAYOUT
        # ==============================================================
        LAYOUTS = ["xy", "yz", "xz", "xy-3d", "yz-3d", "xz-3d", "4panel", "3d"]
        layout = parsers.add_parser('layout', help='Layout')
        layout.set_defaults(func=self.layout)
        layout.add_argument(
            dest='layout', nargs='*', choices=LAYOUTS, help='Layout')
        layout.add_argument(
            '--stack', choices=("row", "column"), help="Stack direction"
        )
        layout.add_argument(
            '--layer', nargs='*', help="Layer(s) to include"
        )
        layout.add_argument(
            '--flex', type=float, default=1, help="Flex"
        )
        layout.add_argument(
            '--append', type=int, nargs='*',
            help="Append to existing (nested) layout"
        )
        layout.add_argument(
            '--insert', type=int, nargs='+',
            help="Insert in existing (nested) layout"
        )
        layout.add_argument(
            '--remove', type=int, nargs='+',
            help="Remove from an existing (nested) layout"
        )

        # ==============================================================
        #   STATE
        # ==============================================================
        state = parsers.add_parser('state', help='Return the viewer\'s state')
        state.set_defaults(func=self.state)
        state.add_argument(
            '--no-print', action='store_false', default=True, dest='print',
            help='Do not print the state.')
        state.add_argument(
            '--save', help='Save JSON state to this file.'
        )
        state.add_argument(
            '--load', help='Load JSON state from this file. '
                           'Can also be a JSON string or a URL.'
        )
        state.add_argument(
            '--url', action='store_true', default=False,
            help='Load (or print) the url form of the state')

        # ==============================================================
        #   EXIT
        # ==============================================================
        exit = parsers.add_parser('exit', aliases=['quit'],
                                  help='Exit neuroglancer')
        exit.set_defaults(func=self.exit)
        return mainparser

    @action
    def display(self, dimension=None):
        """
        Change displayed dimensions.

        Parameters
        ----------
        dimension : str or list[str]
            The three dimensions to display.
            Each dimension can be one of:

            * `"left"` or `"right"`
            * `"posterior"` or `"anterior"`
            * `"inferior"` or `"superior"`

            Otherwise, dimensions can be native axis names of the loaded
            data, such as `"x"`, `"y"`, `"z"`.

            A compact representation (`"RAS"`, or `"zyx"`) can also be
            provided.

        """
        dimensions = ensure_list(dimension or [])
        if dimensions:
            if len(dimensions) == 1:
                dimensions = list(dimensions[0])
            if len(dimensions) != 3:
                raise ValueError('display takes three axis names')
            dimensions = [letter2full.get(letter.lower(), letter)
                          for letter in dimensions]
            self.display_dimensions = dimensions

        def compactNames(names):
            names = list(map(lambda x: x[0].lower(), names))
            names = ''.join([d for d in names if d not in 'ct'])
            return names

        names = compactNames(self.display_dimensions)

        def getDimensions(source, _reentrant=False):
            if getattr(source, 'transform', None):
                transform = source.transform
                if transform.inputDimensions:
                    return transform.inputDimensions
            if getattr(source, 'dimensions', None):
                return source.dimensions
            if not _reentrant and not isinstance(source, ng.LocalVolume):
                mysource = RemoteSource.from_filename(source.url)
                return getDimensions(mysource, True)
            return None

        def getTransform(source):
            if getattr(source, 'transform', None):
                return source.transform
            if not isinstance(source, ng.LocalVolume):
                mysource = RemoteSource.from_filename(source.url)
                if getattr(mysource, 'transform', None):
                    return mysource.transform
            return None

        def reorientUsingTransform(source):
            transform = getTransform(source)
            idims = getDimensions(source)
            matrix = transform.matrix
            if matrix is None:
                matrix = np.eye(4)
            odims = transform.outputDimensions
            onames = transform.outputDimensions.names
            onames = compactNames(onames)
            if all(name in onames for name in names):
                return False
            T0 = ng.CoordinateSpaceTransform(
                matrix=matrix,
                input_dimensions=idims,
                output_dimensions=odims,
            )
            T = neurotransforms[(onames, names)]
            source.transform = compose(T, T0)
            return True

        def reorientUsingDimensions(source):
            idims = getDimensions(source)
            if not idims:
                return False
            inames = compactNames(idims.names)
            if all(name in inames for name in names):
                return True
            T0 = ng.CoordinateSpaceTransform(
                input_dimensions=idims,
                output_dimensions=idims,
            )
            T = neurotransforms[(inames, names)]
            source.transform = compose(T, T0)
            return True

        with self.viewer.txn() as state:
            for layer in state.layers:
                layer = layer.layer
                if isinstance(layer, ng.ImageLayer):
                    for source in layer.source:
                        if getTransform(source):
                            reorientUsingTransform(source)
                        else:
                            reorientUsingDimensions(source)

        self.redisplay()

    def redisplay(self, *args):
        """
        Resets `displayDimensions` to its current value, or to a new value.
        This function does not transform the data accordingly. It only
        sets the state's `displayDimensions`.

        Parameters
        ----------
        dimensions : None or list[str], optional
        """
        if args:
            self.display_dimensions = args[0]
        with self.viewer.txn() as state:
            state.displayDimensions = self.display_dimensions

    @action
    def load(self, filename, name=None, transform=None):
        """
        Load file(s)

        Parameters
        ----------
        filenames : str or list[str]
            Paths or URL, eventually prepended with "type" and "format"
            protocols. Ex: `"labels://nifti://http://cloud.com/path/to/nii.gz"`
        name : str, optional
            A name for the layer.
        transform : array_like or list[float] or str, optional
            Affine transform to apply to the loaded volume.
        """
        filenames = ensure_list(filename or [])
        if name and len(filenames) > 1:
            raise ValueError('Cannot give a single name to multiple layers. '
                             'Use separate `load` calls.')
        name0 = name
        display_dimensions = self.display_dimensions
        self.redisplay(None)

        for filename in filenames or []:
            layertype, format, filename = self.parse_filename(filename)
            name = name0 or os.path.basename(filename)

            if format in self.NG_FORMATS:
                # Remote source
                filename = self.ensure_url(filename)
                source = RemoteSource.from_filename(format + '://' + filename)
                controls = None
                if hasattr(source, 'quantiles'):
                    mn, q0, q1, mx = source.quantiles([0.0, 0.01, 0.99, 1.0])
                    controls = {
                        "normalized": {
                            "range": np.stack([q0, q1]),
                            "window": np.stack([mn, mx]),
                        }
                    }
                layer = ng.ImageLayer(
                    source=ng.LayerDataSource(
                        url=format + '://' + filename,
                        transform=getattr(source, 'transform', None),
                    ),
                    shaderControls=controls,
                )

            elif format in self.EXTRA_FORMATS:
                # Local source
                if format:
                    filename = format + '://' + filename
                source = LocalSource.from_filename(
                    filename, layer_type=layertype)
                controls = None
                if hasattr(source, 'quantiles'):
                    mn, q0, q1, mx = source.quantiles([0.0, 0.01, 0.99, 1.0])
                    controls = {
                        "normalized": {
                            "range": np.stack([q0, q1]),
                            "window": np.stack([mn, mx]),
                        }
                    }
                layer = ng.ImageLayer(
                    source=source,
                    shaderControls=controls,
                )

            else:
                raise ValueError(
                    'Unrecognized format. Try specifying a format with the '
                    'protocol syntax.'
                )

            with self.viewer.txn() as state:
                state.layers.append(name=name, layer=layer)

        if transform:
            self.transform(transform, name=name0)
        self.redisplay(display_dimensions)

    @action
    def unload(self, layer=None):
        """
        Unload layers

        Parameters
        ----------
        layer : str or list[str]
            Layer(s) to unload
        """
        layers = layer
        if not layers:
            with self.viewer.txn() as state:
                layers = [layer.name for layer in state.layers]
        layers = ensure_list(layers)
        with self.viewer.txn() as state:
            for name in layers:
                del state.layers[name]

    @action
    def transform(self, transform, layer=None, inv=False,
                  *, mov=None, fix=None):
        """
        Apply an affine transform

        Parameters
        ----------
        transform : list[float] or np.ndarray or fileobj
            Affine transform (RAS+)
        layer : str or list[str]
            Layer(s) to transform
        inv : bool
            Invert the transform

        Other Parameters
        ----------------
        mov : str
            Moving/Floating image (required by some affine formats)
        fix : str
            Fixed/Reference image (required by some affine formats)
        """
        layer_names = layer or []
        if not isinstance(layer_names, (list, tuple)):
            layer_names = [layer_names]
        display_dimensions = self.display_dimensions
        self.display('ras')

        # prepare transformation matrix
        transform = ensure_list(transform)
        if len(transform) == 1:
            transform = transform[0]
        if isinstance(transform, str):
            transform = load_affine(transform, moving=mov, fixed=fix)
        transform = np.asarray(transform, dtype='float64')
        if transform.ndim == 1:
            if len(transform) == 12:
                transform = transform.reshape([3, 4])
            elif len(transform) == 16:
                transform = transform.reshape([4, 4])
            else:
                n = int(np.sqrt(1 + 4 * len(transform)).item()) // 2
                transform = transform.reshape([n, n+1])
        elif transform.ndim > 2:
            raise ValueError('Transforms must be matrices')
        transform = to_square(transform)
        if inv:
            transform = np.linalg.inv(transform)
        transform = transform[:-1]

        # make ng transform
        T = ng.CoordinateSpaceTransform(
            matrix=transform,
            input_dimensions=ng.CoordinateSpace(
                names=["right", "anterior", "superior"],
                units=["mm"] * 3,
                scales=[1] * 3
            ),
            output_dimensions=ng.CoordinateSpace(
                names=["right", "anterior", "superior"],
                units=["mm"] * 3,
                scales=[1] * 3
            ),
        )

        def getDimensions(source, _reentrant=False):
            if getattr(source, 'transform', None):
                transform = source.transform
                if transform.inputDimensions:
                    return transform.inputDimensions
            if getattr(source, 'dimensions', None):
                return source.dimensions
            if not _reentrant and not isinstance(source, ng.LocalVolume):
                mysource = RemoteSource.from_filename(source.url)
                return getDimensions(mysource, True)
            return None

        def getTransform(source):
            if getattr(source, 'transform', None):
                return source.transform
            if not isinstance(source, ng.LocalVolume):
                mysource = RemoteSource.from_filename(source.url)
                if getattr(mysource, 'transform', None):
                    return mysource.transform
            return None

        def composeTransform(source):
            transform = getTransform(source)
            idims = getDimensions(source)
            matrix = transform.matrix
            if matrix is None:
                matrix = np.eye(4)
            odims = transform.outputDimensions
            T0 = ng.CoordinateSpaceTransform(
                matrix=matrix,
                input_dimensions=idims,
                output_dimensions=odims,
            )
            source.transform = compose(T, T0)
            return True

        def applyTransform(source):
            idims = getDimensions(source)
            if not idims:
                return False
            T0 = ng.CoordinateSpaceTransform(
                input_dimensions=idims,
                output_dimensions=idims,
            )
            source.transform = compose(T, T0)
            return True

        with self.viewer.txn() as state:
            for layer in state.layers:
                if layer_names and layer.name not in layer_names:
                    continue
                layer = layer.layer
                if isinstance(layer, ng.ImageLayer):
                    for source in layer.source:
                        if getTransform(source):
                            composeTransform(source)
                        else:
                            applyTransform(source)

        self.display(display_dimensions)

    @action
    def shader(self, shader, layer=None):
        """
        Apply a shader (that is, a colormap or lookup table)

        Parameters
        ----------
        shader : str
            A known shader name (from `ngtools.shaders`), or some
            user-defined shader code.
        layer : str or list[str], optional
            Apply the shader to these layers. Default: all layers.
        """
        layer_names = layer or []
        if not isinstance(layer_names, (list, tuple)):
            layer_names = [layer_names]
        if hasattr(shaders, shader):
            shader = getattr(shaders, shader)
        elif hasattr(colormaps, shader):
            shader = shaders.colormap(shader)
        with self.viewer.txn() as state:
            for layer in state.layers:
                if layer_names and layer.name not in layer_names:
                    continue
                layer = layer.layer
                layer.shader = shader

    @action
    def state(self, load=None, save=None, url=False, print=True):
        """
        Print or save or load the viewer's JSON state

        Parameters
        ----------
        load : str
            Load state from JSON file, or JSON string (or URL if `url=True`).
        save : str
            Save state to JSON file
        url : bool
            Print/load a JSON URL rather than a JSON object
        print : bool
            Print the JSON object or URL

        Returns
        -------
        state : dict
            JSON state
        """
        if load:
            if os.path.exists(load) or load.startswith(remote_protocols()):
                with open(load) as f:
                    state = json.load(f)
            elif url:
                if '://' in url:
                    url = urlparse(url).fragment
                    if url[0] != '!':
                        raise ValueError('Neuroglancer URL not recognized')
                    url = url[1:]
                state = json.loads(urlunquote(url))
            else:
                state = json.loads(url)
            with self.viewer.txn() as ngstate:
                ngstate.set_state(state)
        else:
            with self.viewer.txn() as ngstate:
                state = ngstate.to_json()

        if save:
            with open(save, 'wb') as f:
                json.dump(state, f, indent=4)

        if print:
            if url:
                state = urlquote(json.dumps(state))
                state = 'https://neuroglancer-demo.appspot.com/#!' + state
                _print(state)
            else:
                _print(json.dumps(state, indent=4))
        return state

    @action
    def layout(self, layout=None, stack=None, layer=None, *,
               flex=1, append=None, insert=None, remove=None):
        """
        Change layout.

        Parameters
        ----------
        layout : [list of] {"xy", "yz", "xz", "xy-3d", "yz-3d", "xz-3d", "4panel", "3d"}
            Layout(s) to set or insert. If list, `stack` must be set.
        stack : {"row", "column"}, optional
            Insert a stack of layouts.
        layer : [list of] str
            Set of layers to include in the layout.
            By default, all layers are included (even future ones).

        Other Parameters
        ----------------
        flex : float, default=1
            ???
        append : bool or [list of] int or str
            Append the layout to an existing stack.
            If an integer or list of integer, they are used to navigate
            through the nested stacks of layouts.
            Only one of append or insert can be used.
        insert : int or [list of] int or str
            Insert the layout into an existing stack.
            If an integer or list of integer, they are used to navigate
            through the nested stacks of layouts.
            Only one of append or insert can be used.
        remove : int or [list of] int or str
            Remove the layout in an existing stack.
            If an integer or list of integer, they are used to navigate
            through the nested stacks of layouts.
            If `remove` is used, `layout` should be `None`.

        Returns
        -------
        layout : object
            Current JSON layout
        """  # noqa: E501
        if not layout and (remove is None):
            with self.viewer.txn() as state:
                print(state.layout)
                return state.layout

        layout = ensure_list(layout or [])

        layer = ensure_list(layer or [])
        if (len(layout) > 1 or stack) and not layer:
            with self.viewer.txn() as state:
                layer = [_.name for _ in state.layers]

        if layer:
            layout = [ng.LayerGroupViewer(
                layers=layer,
                layout=L,
                flex=flex,
            ) for L in layout]

        if len(layout) > 1 and not stack:
            stack = 'row'
        if not stack and len(layout) == 1:
            layout = layout[0]
        if stack:
            layout = ng.StackLayout(
                type=stack,
                children=layout,
                # flex=flex,
            )

        indices = []
        do_append = append is not None
        if do_append:
            indices = ensure_list(append or [])
            append = do_append

        if insert:
            indices = ensure_list(insert or [])
            insert = indices.pop(-1)
        else:
            insert = False

        if remove:
            indices = ensure_list(remove or [])
            remove = indices.pop(-1)
        else:
            remove = False

        if bool(append) + bool(insert) + bool(remove) > 1:
            raise ValueError('Cannot use both append and insert')
        if layout and remove:
            raise ValueError('Do not set `layout` and `remove`')

        with self.viewer.txn() as state:
            if append or (insert is not False) or (remove is not False):
                parent = state.layout
                while indices:
                    parent = layout.children[indices.pop(0)]
                if layout and not isinstance(layout, ng.LayerGroupViewer):
                    if not layer:
                        if len(parent.children):
                            layer = [L for L in parent.children[-1].layers]
                        else:
                            layer = [_.name for _ in state.layers]
                    layout = ng.LayerGroupViewer(
                        layers=layer,
                        layout=layout,
                        flex=flex,
                    )
                if append:
                    parent.children.append(layout)
                elif insert:
                    parent.children.insert(insert, layout)
                elif remove:
                    del parent.children[remove]
            else:
                state.layout = layout
            return state.layout

    @action
    def help(self, action=None):
        """
        Display help

        Parameters
        ----------
        action : str
            Action for which to display help
        """
        if action:
            self.parser.parse_args([action, '--help'])
        else:
            self.parser.parse_args(['--help'])

    @action
    def exit(self):
        """Exit gracefully"""
        del self.fileserver
        sys.exit()

    def ensure_url(self, filename):
        if not filename.startswith(remote_protocols()):
            if filename.startswith('/'):
                filename = 'root://' + filename
            prefix = f'http://{self.fileserver.ip}:{self.fileserver.port}/'
            filename = prefix + filename
        return filename

    # Protocols that describe the type of data contained in the file
    LAYERTYPES = [
        'volume',           # Raster data (image or volume)
        'labels',           # Integer raster data, interpreted as labels
        'surface',          # Triangular mesh
        'tracts',           # Set of piecewise curves
        'roi',              # Region of interest ???
        'points',           # Pointcloud
        'transform',        # Spatial transform
        'affine',           # Affine transform
    ]

    # Native neuroglancer formats
    NG_FORMATS = [
        'boss',             # bossDB: Block & Object storage system
        'brainmap',         # Google Brain Maps
        'deepzoom',         # Deep Zoom file-backed data source
        'dvid',             # DVID
        'graphene',         # Graphene Zoom file-backed data source
        'local',            # Local in-memory
        'n5',               # N5 data source
        'nggraph',          # nggraph data source
        'nifti',            # Single NIfTI file
        'obj',              # Wavefront OBJ mesh file
        'precomputed',      # Precomputed file-backed data source
        'render',           # Render
        'vtk',              # VTK mesh file
        'zarr',             # Zarr data source
        'zarr2',            # Zarr v2 data source
        'zarr3',            # Zarr v3 data source
    ]

    # Extra local formats (if not specified, try to guess from file)
    EXTRA_FORMATS = [
        'mgh',              # Freesurfer volume format
        'mgz',              # Freesurfer volume format (compressed)
        'trk',              # Freesurfer streamlines
        'lta',              # Freesurfer affine transform
        'surf',             # Freesurfer surfaces
        'annot',            # Freesurfer surface annotation
        'tck',              # MRtrix streamlines
        'mif',              # MRtrix volume format
        'gii',              # Gifti
        'tiff',             # Tiff volume format
        'niftyreg',         # NiftyReg affine transform
    ]

    def parse_filename(self, filename):

        datatype = None
        for dt in self.LAYERTYPES:
            if filename.startswith(dt + '://'):
                datatype = dt
                filename = filename[len(dt)+3:]
                break

        format = None
        for fmt in self.NG_FORMATS + self.EXTRA_FORMATS:
            if filename.startswith(fmt + '://'):
                format = fmt
                filename = filename[len(fmt)+3:]
                break

        if format is None:
            if filename.endswith('.mgh'):
                format = 'mgh'
            elif filename.endswith('.mgz'):
                format = 'mgz'
            elif filename.endswith(('.nii', '.nii.gz')):
                format = 'nifti'
            elif filename.endswith('.trk'):
                format = 'trk'
            elif filename.endswith('.tck'):
                format = 'tck'
            elif filename.endswith('.lta'):
                format = 'lta'
            elif filename.endswith('.mif'):
                format = 'mif'
            elif filename.endswith(('.tiff', '.tif')):
                format = 'tiff'
            elif filename.endswith('.gii'):
                format = 'gii'
            elif filename.endswith(('.zarr', '.zarr/')):
                format = 'zarr'
            elif filename.endswith('.vtk'):
                format = 'vtk'
            elif filename.endswith('.obj'):
                format = 'obj'
            elif filename.endswith(('.n5', '.n5/')):
                format = 'n5'

        return datatype, format, filename


class NoExitArgParse(argparse.ArgumentParser):
    def exit(self, status=0, message=None):
        pass

    def error(self, message):
        pass


class UserInputCompleter:

    RE_SPACE = re.compile(r'.*\s+$', re.M)
    HISTORY = os.path.expanduser('~/.neuroglancer_history')

    def __init__(self, commands=tuple(), ):
        self.commands = commands
        readline.set_completer_delims(' \t\n;')
        readline.parse_and_bind('tab: complete')
        readline.set_completer(self.complete)
        if not os.path.exists(self.HISTORY):
            with open(self.HISTORY, 'wt'):
                pass
        readline.read_history_file(self.HISTORY)
        atexit.register(self._save_history, self.HISTORY)

    def _save_history(self, histfile):
        readline.set_history_length(1000)
        readline.write_history_file(histfile)

    def _listdir(self, root):
        "List directory 'root' appending the path separator to subdirs."
        res = []
        for name in os.listdir(root):
            path = os.path.join(root, name)
            if os.path.isdir(path):
                name += os.sep
            res.append(name)
        return res

    def _complete_path(self, path=None):
        "Perform completion of filesystem path."
        if not path:
            return self._listdir('.')
        dirname, rest = os.path.split(path)
        tmp = dirname if dirname else '.'
        res = [os.path.join(dirname, p)
               for p in self._listdir(tmp) if p.startswith(rest)]
        # more than one match, or single match which does not exist (typo)
        if len(res) > 1 or not os.path.exists(path):
            return res
        # resolved to a single directory, so return list of files below it
        if os.path.isdir(path):
            return [os.path.join(path, p) for p in self._listdir(path)]
        # exact file match terminates this completion
        return [path + ' ']

    def complete_default(self, args):
        if not args:
            return self._complete_path('.')
        # treat the last arg as a path and complete it
        return self._complete_path(os.path.expanduser(args[-1]))

    def complete(self, text, state):
        "Generic readline completion entry point."
        buffer = readline.get_line_buffer()
        line = readline.get_line_buffer().split()
        # show all commands
        if not line:
            return [c + ' ' for c in self.commands][state]
        # account for last argument ending in a space
        if self.RE_SPACE.match(buffer):
            line.append('')
        # resolve command to the implementation function
        cmd = line[0].strip()
        if cmd in self.commands:
            impl = getattr(self, 'complete_%s' % cmd, self.complete_default)
            args = line[1:]
            if args:
                return (impl(args) + [None])[state]
            return [cmd + ' '][state]
        results = [c + ' ' for c in self.commands if c.startswith(cmd)]
        results += [None]
        return results[state]
