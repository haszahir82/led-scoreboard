"""Panel geometry and the cell grid.

Every screen in this project is drawn for a 64x32 cell. That is not laziness:
at this pixel density a game needs two 20px logos and a 23px centre column, and
those numbers do not scale by stretching.

So when there is more room, from daisy-chained panels or a 64x64 panel, the
right move is more cells rather than bigger ones. A 128x32 chain of two shows
two games side by side. A 128x64 shows four. That is what the extra panels are
actually good for, and it means no layout has to change.

    +---------------+---------------+
    |  OSU 21 MICH  |  UGA 10 BAMA  |   128x32, chain_length 2
    +---------------+---------------+
"""

CELL_W = 64
CELL_H = 32


class Geometry:
    """Total panel size, and the grid of 64x32 cells that fits inside it."""

    def __init__(self, rows=32, cols=64, chain_length=1, parallel=1,
                 tile=True):
        self.panel_rows = int(rows)
        self.panel_cols = int(cols)
        self.chain_length = max(1, int(chain_length))
        self.parallel = max(1, int(parallel))

        self.width = self.panel_cols * self.chain_length
        self.height = self.panel_rows * self.parallel

        # Only tile when whole cells fit. An odd size (a single 32x32, say)
        # falls back to one cell covering the panel, and layouts simply draw
        # what fits.
        fits = (self.width >= CELL_W and self.height >= CELL_H)
        self.tiled = bool(tile) and fits

        if self.tiled:
            self.cols = max(1, self.width // CELL_W)
            self.rows = max(1, self.height // CELL_H)
            self.cell_width = CELL_W
            self.cell_height = CELL_H
        else:
            self.cols = 1
            self.rows = 1
            self.cell_width = self.width
            self.cell_height = self.height

    @property
    def cell_count(self):
        return self.cols * self.rows

    @property
    def single(self):
        return self.cell_count == 1

    def cell_origin(self, index):
        """Top-left pixel of cell `index`, filling left to right, top to bottom."""
        col = index % self.cols
        row = (index // self.cols) % self.rows
        return col * self.cell_width, row * self.cell_height

    def describe(self):
        panel = "{}x{}".format(self.panel_cols, self.panel_rows)
        layout = "{}x{}".format(self.width, self.height)
        chain = []
        if self.chain_length > 1:
            chain.append("chain {}".format(self.chain_length))
        if self.parallel > 1:
            chain.append("parallel {}".format(self.parallel))
        suffix = " ({})".format(", ".join(chain)) if chain else ""
        cells = "{} cell{}".format(self.cell_count,
                                   "" if self.cell_count == 1 else "s")
        return "{} panel -> {}{}, {}".format(panel, layout, suffix, cells)


def from_options(options):
    """Build geometry from the exact options handed to the matrix library.

    This is the one that main.py uses, and it exists because building the two
    independently let them disagree. The library sanity-checks what it is given
    and silently corrects some of it -- an Adafruit HAT cannot drive parallel
    chains, so parallel=2 becomes 1 -- and geometry reading the raw config
    instead would go on believing the panel was twice as tall as it is, then
    draw cells into rows that do not exist.

    One source of truth. If the library was told 128x32, this says 128x32.
    """
    return Geometry(
        rows=options.get("rows") or 32,
        cols=options.get("cols") or 64,
        chain_length=options.get("chain_length") or 1,
        parallel=options.get("parallel") or 1,
        tile=options.get("tile_screens", True),
    )


def from_config(config, overrides=None):
    """Build geometry from config, with CLI overrides taking precedence."""
    overrides = overrides or {}

    def pick(key, config_key=None):
        value = overrides.get(key)
        if value is not None:
            return value
        return config.get("matrix.{}".format(config_key or key))

    return Geometry(
        rows=pick("rows") or 32,
        cols=pick("cols") or 64,
        chain_length=pick("chain_length") or 1,
        parallel=pick("parallel") or 1,
        tile=config.get("matrix.tile_screens", True),
    )
