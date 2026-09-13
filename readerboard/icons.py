"""The built-in icons a message draws with ``<icon:name>``.

An icon is a bitmap seven dots high, which is the whole height of the display,
and between three and twelve dots wide. It is drawn into a SMALL DOTS PICTURE
file and called inline, so ``<icon:lock:red> DOOR LOCKED`` puts a padlock in
front of the words at the cost of two bytes in the message.

These are data rather than code, and the way data goes wrong is quietly. A row
one dot short, a letter that is not an ink, a name that cannot sit inside a tag:
none of those raises anything on import and all of them reach the sign.
``tests/test_icons.py`` is what stops that, and it pins the shape of every icon
here rather than sampling.

Why the set looks the way it does
=================================

Every icon below has been drawn on the sign and read correctly by a person who
was told to say what they saw rather than what it should have been. That took
four sessions across 2026-09-11 and 2026-09-12, and about a fifth of what was
drawn did not survive them: an atom, a DNA helix, a chip, a soccer ball, a gear,
a globe. They were the detailed ones. Seven rows carry a shape, not a picture of
one, and colour separates things far better than detail does at this size. A
redraw usually rescued a failure; the lion, robot, alien and snowman were each
tried twice and then given up.

So the bar for a new icon is not that it looks right on a screen. It is a single
bold outline with at most one feature inside it, and it has been on the sign.

How one is stored
=================

Seven rows, one character a dot. A ``.`` is unlit and the letters are inks: an
icon drawn in fixed colours names each one, and an icon that takes a tint writes
``#`` for the ink the caller chooses and keeps its own as the default. So
``check`` is green unless a message asks for ``<icon:check:red>``, and ``sun``
is amber and yellow whatever anybody asks.

The tint words are the colour tokens' own, down to ``dimred`` and ``dimgreen``,
because a message that can say ``<red>`` should not have to learn a second
spelling to say ``<icon:check:red>``.
"""

from __future__ import annotations

from dataclasses import dataclass

# Every icon is exactly this tall, which is the height of the display.
ICON_HEIGHT = 7

# The widest an icon may be, and the width every picture file is allocated at.
# A picture narrower than its file draws narrow rather than padded, so one
# width for the pool costs nothing and keeps the layout to a single number.
ICON_MAX_WIDTH = 16

# The character standing in for whichever ink the caller chooses.
INK_MARKER = "#"

# Each ink, and the Table 22 pixel code the sign draws it from.
INK_CODES = {
    ".": "0",
    "R": "1",
    "G": "2",
    "A": "3",
    "r": "4",
    "g": "5",
    "b": "6",
    "o": "7",
    "y": "8",
}

# The word a message writes for each ink, matching the colour tokens exactly.
TINT_INKS = {
    "red": "R",
    "green": "G",
    "amber": "A",
    "dimred": "r",
    "dimgreen": "g",
    "brown": "b",
    "orange": "o",
    "yellow": "y",
}

# What an icon may be called. The same shape a variable name takes, because an
# icon name has to sit inside a tag too.
ICON_NAME_PATTERN = r"^[a-z0-9_]{1,32}$"


class UnknownIcon(ValueError):
    """An icon was asked for that this service does not have."""


class UntintableIcon(ValueError):
    """A tint was asked for on an icon drawn in fixed colours."""


class UnknownTint(ValueError):
    """A tint was asked for that is not one of the sign's eight inks."""


@dataclass(frozen=True, slots=True)
class Icon:
    """One icon: what it is called, what it belongs with, and how it is drawn."""

    name: str
    group: str
    tint: str | None
    rows: tuple[str, ...]

    @property
    def width(self) -> int:
        """How many dots wide it is drawn."""
        return len(self.rows[0])

    @property
    def tintable(self) -> bool:
        """Whether a caller may choose its ink.

        False for an icon whose colours are the point of it: a sun is amber and
        yellow, and a green one is not a sun.
        """
        return self.tint is not None

    def pixels(self, tint: str | None = None) -> list[str]:
        """Return the rows as Table 22 pixel codes, ready for a picture write.

        ``tint`` is a colour word, and passing one for an icon drawn in fixed
        colours is refused rather than ignored: a caller that asked for green
        should be told this icon has no single ink, not handed the icon it did
        not ask for.
        """
        ink = self.tint
        if tint is not None:
            if not self.tintable:
                raise UntintableIcon(
                    "<icon:%s> is drawn in fixed colours and takes no tint; write "
                    "<icon:%s>" % (self.name, self.name)
                )
            ink = TINT_INKS.get(tint)
            if ink is None:
                raise UnknownTint(
                    "%r is not a colour; use one of %s"
                    % (tint, ", ".join(sorted(TINT_INKS)))
                )
        codes = dict(INK_CODES)
        if ink is not None:
            codes[INK_MARKER] = INK_CODES[ink]
        # An icon with no ink has no marker in it to fill either, which
        # tests/test_icons.py pins for every one of them.
        return ["".join(codes[dot] for dot in row) for row in self.rows]


# The icons themselves, by group, in the order a person reading a list of them
# would want. Each entry is its name, the ink it takes by default or None if its
# colours are fixed, and its seven rows.
_TABLE: tuple[tuple[str, tuple[tuple[str, str | None, tuple[str, ...]], ...]], ...] = (
    ("status", (
        ("check", "G", (".......", "......#", ".....##", "#...##.", "##.##..", ".###...", "..#....")),
        ("cross", "R", ("#.....#", "##...##", ".##.##.", "..###..", ".##.##.", "##...##", "#.....#")),
        ("dot", "G", (".....", ".###.", "#####", "#####", "#####", ".###.", ".....")),
        ("stop", "R", (".#####.", "#######", "#######", "#.....#", "#######", "#######", ".#####.")),
        ("warn", "y", (
            "....#....", "...###...", "...#.#...", "..##.##..",
            "..#####..", ".###.###.", "#########",
        )),
        ("bell", "A", ("...#...", "..###..", ".#####.", ".#####.", ".#####.", "#######", "...#...")),
        ("clock", "A", (".#####.", "#..#..#", "#..#..#", "#..##.#", "#.....#", "#.....#", ".#####.")),
    )),
    ("arrows", (
        ("up", "A", ("...#...", "..###..", ".#####.", "#######", "..###..", "..###..", "..###..")),
        ("down", "A", ("..###..", "..###..", "..###..", "#######", ".#####.", "..###..", "...#...")),
        ("left", "A", ("...#....", "..##....", ".#######", "########", ".#######", "..##....", "...#....")),
        ("right", "A", ("....#...", "....##..", "#######.", "########", "#######.", "....##..", "....#...")),
    )),
    ("weather", (
        ("sun", None, (
            "o...o...o", ".o.....o.", "...yyy...", "oo.yyy.oo",
            "...yyy...", ".o.....o.", "o...o...o",
        )),
        ("cloud", "y", (
            ".........", "..###.##.", ".########", "#########",
            "#########", ".#######.", ".........",
        )),
        ("rain", None, (
            "..yyy.yy.", ".yyyyyyyy", "yyyyyyyyy", ".yyyyyyy.",
            ".........", ".G..G..G.", "G..G..G..",
        )),
        ("snow", "y", ("...#...", ".#.#.#.", "..###..", "#######", "..###..", ".#.#.#.", "...#...")),
        ("bolt", "y", ("..###", ".###.", ".##..", "#####", "..##.", ".##..", ".#...")),
        ("moon", "y", ("..###.", ".##...", "##....", "##....", "##....", ".##...", "..###.")),
        ("temp", None, ("..A..", ".A.A.", ".ARA.", ".ARA.", "ARRRA", "ARRRA", ".AAA.")),
    )),
    ("home", (
        ("house", None, (
            "....R....", "...RRR...", "..RRRRR..", ".RRRRRRR.",
            "..AAAAA..", "..AA.AA..", "..AA.AA..",
        )),
        ("lock", "A", ("..###..", ".#...#.", ".#...#.", "#######", "###.###", "###.###", "#######")),
        ("unlock", "A", ("..###..", ".#...#.", ".#.....", "#######", "###.###", "###.###", "#######")),
        ("bulb", None, (".yyy.", "yyyyy", "yyyyy", "yyyyy", ".yyy.", ".bbb.", ".bbb.")),
        ("coffee", None, (
            ".y..y...", "..y..y..", "bbbbbb..", "bbbbbbb.",
            "bbbbb.b.", "bbbbbbb.", ".bbbb...",
        )),
    )),
    ("power and signal", (
        ("batt_full", None, (
            "..........", "yyyyyyyyy.", "yGGGGGGGyy", "yGGGGGGGyy",
            "yGGGGGGGyy", "yyyyyyyyy.", "..........",
        )),
        ("batt_half", None, (
            "..........", "yyyyyyyyy.", "yoooo...yy", "yoooo...yy",
            "yoooo...yy", "yyyyyyyyy.", "..........",
        )),
        ("batt_low", None, (
            "..........", "yyyyyyyyy.", "yRR.....yy", "yRR.....yy",
            "yRR.....yy", "yyyyyyyyy.", "..........",
        )),
        ("signal", "G", ("......#", "....#.#", "....#.#", "..#.#.#", "..#.#.#", "#.#.#.#", "#.#.#.#")),
    )),
    ("messages and fun", (
        ("mail", "A", (
            "#########", "##.....##", "#.#...#.#", "#..#.#..#",
            "#...#...#", "#.......#", "#########",
        )),
        ("note", "A", ("..#..", "..##.", "..#.#", "..#..", "###..", "###..", ".....")),
        ("notes", "A", ("..#####", "..#...#", "..#...#", "..#...#", "###.###", "###.###", ".......")),
        ("heart", "R", (".##.##.", "#######", "#######", ".#####.", "..###..", "...#...", ".......")),
        ("star", "y", ("...#...", "...#...", "..###..", "#######", "..###..", "...#...", "...#...")),
        ("smile", "y", (".#####.", "#######", "##.#.##", "#######", "#.###.#", "##...##", ".#####.")),
        ("frown", "y", (".#####.", "#######", "##.#.##", "#######", "##...##", "#.###.#", ".#####.")),
    )),
    ("around the house", (
        ("trash", "G", ("..###..", "#######", ".#####.", ".#.#.#.", ".#.#.#.", ".#.#.#.", ".#####.")),
        ("key", "y", (
            ".........", ".###.....", "#...#....", "#...#####",
            "#...#.#.#", ".###.....", ".........",
        )),
        ("calendar", None, (".b...b.", "RRRRRRR", "RRRRRRR", "yyyyyyy", "yyyRyyy", "yyyyyyy", "yyyyyyy")),
        ("camera", "R", (
            "..###....", "#########", "###yyy###", "##y...y##",
            "###yyy###", "#########", ".........",
        )),
        ("speaker", "A", (
            "...#..#.", "..##...#", "####.#.#", "####.#.#",
            "####.#.#", "..##...#", "...#..#.",
        )),
    )),
    ("climate", (
        ("flame", None, ("..R..", ".RRR.", ".RoR.", "RRoRR", "RoyoR", "RoyoR", ".RRR.")),
        ("drop", "y", ("..#..", "..#..", ".###.", ".###.", "#####", "#####", ".###.")),
        ("wind", "y", (
            ".....###.", "........#", "########.", ".........",
            "######...", "......#..", ".....#...",
        )),
    )),
    ("media", (
        ("play", "G", ("#...", "##..", "###.", "####", "###.", "##..", "#...")),
        ("pause", "A", (".....", "##.##", "##.##", "##.##", "##.##", "##.##", ".....")),
        ("skip", "A", (".......", "#..#..#", "##.##.#", "#######", "##.##.#", "#..#..#", ".......")),
        ("back", "A", (".......", "#..#..#", "#.##.##", "#######", "#.##.##", "#..#..#", ".......")),
        ("headphones", "A", (".#####.", "#.....#", "#.....#", "#.....#", "##...##", "##...##", "##...##")),
        ("tv", "A", (
            "#########", "#yyyyyyy#", "#yyyyyyy#", "#yyyyyyy#",
            "#########", "...###...", ".#######.",
        )),
        ("gamepad", "A", (
            "...........", ".#########.", "##G#####R##", "#GGG###R#R#",
            "##G#####R##", "####...####", "###.....###",
        )),
    )),
    ("symbols", (
        ("plus", "G", (".....", "..#..", "..#..", "#####", "..#..", "..#..", ".....")),
        ("minus", "R", (".....", ".....", ".....", "#####", ".....", ".....", ".....")),
        ("question", "y", (".###.", "#...#", "....#", "..##.", "..#..", ".....", "..#..")),
        ("info", "A", (".#.", "...", "##.", ".#.", ".#.", ".#.", "###")),
        ("wifi", "G", (
            ".#######.", "#.......#", "..#####..", ".#.....#.",
            "...###...", ".........", "....#....",
        )),
        ("flag", "R", ("b####.", "b#####", "b####.", "b.....", "b.....", "b.....", "b.....")),
        ("leaf", "G", ("....###", "..###.#", ".###.##", ".##.###", "##.###.", "#.###..", "#......")),
        ("dollar", "G", ("..#..", ".####", "#.#..", ".###.", "..#.#", "####.", "..#..")),
        ("trend_up", "G", ("...####", ".....##", "....#.#", "...#...", "..#....", ".#.....", "#......")),
        ("trend_down", "R", ("#......", ".#.....", "..#....", "...#...", "....#.#", ".....##", "...####")),
    )),
    ("science and tech", (
        ("flask", "y", ("..###..", "..#.#..", "..#.#..", ".#...#.", ".#GGG#.", "#GGGGG#", "#######")),
        ("magnet", None, (".RRRRR.", "RRRRRRR", "RR...RR", "RR...RR", "RR...RR", "yy...yy", "yy...yy")),
        ("pulse", "G", (
            "....#......", "....#......", "...#.#.....", "####.#..###",
            ".....#.#...", "......#....", "...........",
        )),
        ("wave", "G", (
            "..##.......", ".#..#......", ".#...#.....", "#....#....#",
            "......#...#", "......#..#.", ".......##..",
        )),
        ("magnifier", "A", (".###...", "#...#..", "#...#..", "#...#..", ".###b..", ".....b.", "......b")),
        ("bug", "R", (".#...#.", "..###..", "#.###.#", ".#####.", "#.###.#", ".#####.", "#.###.#")),
        ("code", "G", (
            ".........", "..#..##..", ".#...#.#.", "#...#...#",
            ".#.#...#.", "..##..#..", ".........",
        )),
        ("terminal", "A", ("#######", "#.....#", "#G....#", "#.G...#", "#G.GGG#", "#.....#", "#######")),
        ("server", "A", (
            "#########", "#.....#G#", "#########", "#.....#G#",
            "#########", "#.....#R#", "#########",
        )),
        ("satellite", None, (
            ".........", "yyy...yyy", "yyy.A.yyy", "yyyAAAyyy",
            "yyy.A.yyy", "yyy...yyy", ".........",
        )),
        ("compass", "A", (".#####.", "#..R..#", "#..R..#", "#..y..#", "#..y..#", "#.....#", ".#####.")),
        ("pi", "y", (".######", "#.#..#.", "..#..#.", "..#..#.", "..#..#.", ".#...#.", "#....##")),
        ("calculator", "A", ("#####", "#GGG#", "#####", "#.#.#", "#####", "#.#.#", "#####")),
    )),
    ("sports", (
        ("tennis", None, (".GGGGG.", "GyGGGyG", "GGyGyGG", "GGyGyGG", "GGyGyGG", "GyGGGyG", ".GGGGG.")),
        ("bowling", None, (".yyy.", ".yyy.", "..y..", ".RRR.", "yyyyy", "yyyyy", ".yyy.")),
        ("dumbbell", "A", (
            ".........", "##.....##", "##.....##", "#########",
            "##.....##", "##.....##", ".........",
        )),
        ("paddle", None, (".RRR...", "RRRRR.y", "RRRRR..", "RRRRR..", ".RRR...", "...bb..", "....bb.")),
        ("finish", None, ("by.y.y.", "b.y.y.y", "by.y.y.", "b.y.y.y", "b......", "b......", "b......")),
    )),
    ("animals", (
        ("cat", "o", ("#.....#", "##...##", "#######", "#G###G#", "###R###", ".#####.", "..###..")),
        ("dog", None, (
            "bb.....bb", "bbAAAAAbb", "bbA.A.Abb", "b.AAAAA.b",
            "..AAbAA..", "...AAA...", "....R....",
        )),
        ("bunny", "y", (".#...#.", ".#...#.", ".#...#.", ".#####.", "##.#.##", "###R###", ".#####.")),
        ("pig", None, ("rr...rr", "rrrrrrr", "r.rrr.r", "rRRRRRr", "rR.R.Rr", "rRRRRRr", ".rrrrr.")),
        ("chick", None, ("..yyy..", ".yyy.yo", ".yyyyy.", "yyyyyy.", "yyyyyy.", ".yyyy..", "..o.o..")),
        ("owl", None, (
            "b.......b", "bbbbbbbbb", "byyybyyyb", "by.yby.yb",
            "byyyoyyyb", ".bbbbbbb.", "..o...o..",
        )),
        ("frog", None, (
            ".GGG.GGG.", ".G.GGG.G.", "GGGGGGGGG", "GRRRRRRRG",
            "GGGGGGGGG", ".GGGGGGG.", "GG.....GG",
        )),
        ("fish", "o", (
            ".........", "..####..#", ".#.######", "########.",
            ".########", "..####..#", ".........",
        )),
        ("octopus", "o", (".#####.", "#######", "#.###.#", "#######", "#######", "#.#.#.#", ".#.#.#.")),
        ("dino", "G", (
            "........###.", "........#.##", "........#...", "..#######...",
            ".########...", "#.######....", "..##..##....",
        )),
        ("butterfly", "o", (
            "##.b.b.##", "###.b.###", "#y##b##y#", ".###b###.",
            "..##b##..", ".#y#b#y#.", ".##.b.##.",
        )),
        ("ladybug", None, ("..bbb..", ".RRbRR.", "R.RbR.R", "RRRbRRR", "RR.b.RR", ".RRbRR.", "..RRR..")),
        ("bee", None, (".b...b.", "..bbb..", "yyAAAyy", "yybbbyy", ".yAAAy.", "..bbb..", "...A...")),
        ("bear", None, ("bb...bb", "bbbbbbb", "b.bbb.b", "bbbbbbb", "bbA.Abb", "bbAAAbb", ".bbbbb.")),
        ("mouse", "A", ("##...##", "##...##", ".#####.", "#.###.#", "#######", ".##R##.", "..###..")),
        ("fox", None, ("o.....o", "oo...oo", "ooooooo", "o.ooo.o", "yyoooyy", ".yybyy.", "..yyy..")),
        ("turtle", None, (
            "...........", "...GGGG....", "..GgGGgG.gg", ".GGGGGGGggg",
            ".ggggggggg.", ".gg....gg..", "...........",
        )),
        ("duck", None, (
            "......yyy.", ".....yy.y.", "y....yyyyo", "yy...yyy..",
            "yyyyyyyyy.", "yyyyyyyyy.", ".yyyyyyy..",
        )),
        ("crab", None, (
            "RR.....RR", "R.R...R.R", ".R.R.R.R.", "..RRRRR..",
            "RRRRRRRRR", ".RRRRRRR.", "R.R...R.R",
        )),
        ("whale", "A", (
            ".y.y.......", "..y.....#.#", ".######..#.", "#.#######..",
            "#########..", ".#######...", "...........",
        )),
        ("bat", "r", (
            "#.........#", "##..#.#..##", "###.###.###", "###########",
            ".#########.", "..#.###.#..", ".....#.....",
        )),
    )),
    ("faces", (
        ("wow", "y", (".#####.", "#######", "##.#.##", "#######", "##...##", "##...##", ".#####.")),
    )),
    ("food and treats", (
        ("ice_cream", None, ("..R..", ".yyy.", "yyyyy", "bbbbb", ".bbb.", ".bbb.", "..b..")),
        ("cupcake", None, ("...R...", "..yyy..", ".yyyyy.", "yyyyyyy", ".bbbbb.", ".bAbAb.", "..bbb..")),
        ("apple", None, ("...bG..", ".RRbRR.", "RRRRRRR", "RRRRRRR", "RRRRRRR", ".RRRRR.", "..R.R..")),
        ("strawberry", None, ("..GGG..", ".RRRRR.", "RyRRRyR", "RRRyRRR", ".RyRyR.", "..RRR..", "...R...")),
        ("pizza", None, (
            "bbbbbbbbb", "yyRyyyRyy", ".yyyyyyy.", ".yyyRyyy.",
            "..yyyyy..", "...yyy...", "....y....",
        )),
        ("banana", None, (
            ".........", ".........", "b.......y", "yy.....yy",
            ".yyyyyyy.", "..yyyyy..", ".........",
        )),
        ("cherries", None, ("....GG.", "...G.G.", "..G..G.", ".G...G.", "RRR.RRR", "RRR.RRR", ".R...R.")),
        ("carrot", None, ("G.G.G", ".GGG.", "ooooo", ".ooo.", ".ooo.", "..o..", "..o..")),
        ("cookie", None, (".AAAAA.", "AA.AAAA", "AAAAA.A", "A.AAAAA", "AAAA.AA", "AA.AAAA", ".AAAAA.")),
        ("lollipop", None, (".RRR.", "RyyyR", "RyRyR", "RyyyR", ".RRR.", "..y..", "..y..")),
        ("donut", "o", (".#####.", "#y##G##", "##...##", "##...##", "##...##", "##G#y##", ".#####.")),
        ("watermelon", None, (
            "...........", "RRRRRRRRRRR", "GRRbRRRbRRG", ".GRRRbRRRG.",
            "..GRRRRRG..", "...GGGGG...", "...........",
        )),
        ("burger", None, (
            "..ooooo..", ".oyoooyo.", "GGGGGGGGG", "yyyyyyyyy",
            "bbbbbbbbb", ".ooooooo.", "..ooooo..",
        )),
        ("fries", None, (".y.y.y.", "yyyyyyy", "RRRRRRR", ".RRRRR.", ".RRRRR.", ".RRRRR.", ".RRRRR.")),
        ("cake", None, (
            "..y.y.y..", "..R.R.R..", "yyyyyyyyy", "bbbbbbbbb",
            "RRRRRRRRR", "bbbbbbbbb", "yyyyyyyyy",
        )),
        ("popcorn", None, (".yy.yy.", "yyyyyyy", "RyRyRyR", "RyRyRyR", ".RyRyR.", ".RyRyR.", ".RyRyR.")),
        ("cheese", None, (".......", "....yyy", "..yyyyy", "yyyy.yy", "yy.yyyy", "yyyyy.y", "yyyyyyy")),
    )),
    ("garden and sky", (
        ("flower", "R", ("..###..", ".##y##.", "..###..", "...G...", ".G.G.G.", "..GGG..", "...G...")),
        ("tree", None, ("..GGG..", ".GGGGG.", "GGGGGGG", "GGGGGGG", ".GGGGG.", "...b...", "..bbb..")),
        ("mushroom", None, ("..RRR..", ".RyRyR.", "RRRRRRR", "RyRRRyR", "..yyy..", "..yyy..", ".yyyyy.")),
        ("rainbow", None, (
            "...RRRRR...", "..RoooooR..", ".RoyyyyyoR.", "RoyGGGGGyoR",
            "RoyG...GyoR", "RoyG...GyoR", "RoyG...GyoR",
        )),
        ("planet", None, (
            "...........", "....ooo....", "...ooooo...", "yyyyyyyyyyy",
            "...ooooo...", "....ooo....", "...........",
        )),
        ("umbrella", "R", (
            "...###...", ".#######.", "#########", "#.#.#.#.#",
            "....b....", "....b....", "..bbb....",
        )),
        ("plant", None, (".GG.GG.", "..GGG..", "...G...", "...G...", "bbbbbbb", ".bbbbb.", ".bbbbb.")),
    )),
    ("toys and play", (
        ("ball", None, ("..RyG..", ".RRyGG.", "RRRyGGG", "RRRyGGG", "RRRyGGG", ".RRyGG.", "..RyG..")),
        ("kite", None, ("...R...", "..RRy..", ".RRRyy.", "..yyR..", "...y...", "....b..", ".....b.")),
        ("dice", "R", ("#######", "#.###.#", "#######", "###.###", "#######", "#.###.#", "#######")),
        ("trophy", "y", ("#######", "#.###.#", "#.###.#", ".#####.", "..###..", "...#...", ".#####.")),
        ("drum", None, (".y...y.", "..y.y..", "yyyyyyy", "RRRRRRR", "RyRyRyR", "RRRRRRR", "yyyyyyy")),
        ("basketball", None, (".ooboo.", "ooobooo", "booboob", "bbbbbbb", "booboob", "ooobooo", ".ooboo.")),
        ("medal", None, ("R...R", ".R.R.", "..R..", ".yyy.", "yyyyy", "yyyyy", ".yyy.")),
    )),
    ("party and pretend", (
        ("balloon", "R", (".###.", "#####", "#####", ".###.", "..#..", "...y.", "..y..")),
        ("crown", None, (
            ".........", "y...y...y", "yy.yyy.yy", "yyyyyyyyy",
            "yRyyRyyRy", "yyyyyyyyy", ".........",
        )),
        ("rocket", None, ("...y...", "..yyy..", "..y.y..", "..yyy..", ".RyyyR.", "RR.o.RR", "...o...")),
        ("ghost", "y", (".#####.", "#######", "##.#.##", "#######", "#######", "#######", "#.#.#.#")),
        ("pumpkin", None, ("...G...", ".ooooo.", "oo.o.oo", "ooooooo", "o.o.o.o", ".ooooo.", ".......")),
        ("castle", "A", (
            "#.#.#.#.#", "#########", "#########", "##.###.##",
            "#########", "###...###", "###...###",
        )),
        ("gem", "R", (".......", ".##y##.", "#######", ".#####.", "..###..", "...#...", ".......")),
        ("wand", None, (".....y.", "....yyy", "....by.", "...b...", "..b....", ".b.....", "b......")),
        ("chest", None, (
            ".bbbbbbb.", "bbbbbbbbb", "yyyyyyyyy", "bbbbybbbb",
            "bbbbbbbbb", "bbbbbbbbb", ".........",
        )),
    )),
    ("holidays", (
        ("xmas_tree", None, ("...y...", "..GGG..", ".GGRGG.", "..GGG..", "GRGGGyG", "GGGGGGG", "...b...")),
        ("gift", "R", (".yy.yy.", "###y###", "###y###", "yyyyyyy", "###y###", "###y###", "###y###")),
        ("candy_cane", None, (".RyR.", "y...R", "R...y", "....R", "....y", "....R", "....y")),
        ("egg", None, (".yyy.", "yyyyy", "RyRyR", "yRyRy", "GGGGG", "yyyyy", ".yyy.")),
    )),
    ("things that go", (
        ("car", "R", (
            "...........", "...#####...", "..#..#..#..", "###########",
            "###########", "##bb###bb##", "..bb...bb..",
        )),
        ("bus", "A", (
            "##########..", "#..#..#..##.", "#..#..#..###", "############",
            "############", "##bb####bb##", "..bb....bb..",
        )),
        ("boat", None, ("...y....", "...yy...", "...yyy..", "...yyyy.", "...b....", "bbbbbbbb", ".bbbbbb.")),
    )),
    ("every day", (
        ("book", None, (
            ".yyy.yyy.", "yyyyyyyyy", "ybbbybbby", "yyyyyyyyy",
            "ybbbybbby", "yyyyyyyyy", "bbbb.bbbb",
        )),
        ("backpack", "R", ("..###..", ".#...#.", "#######", "#######", "##yyy##", "##yyy##", "#######")),
    )),
)

# Every icon, by name.
ICONS: dict[str, Icon] = {
    name: Icon(name=name, group=group, tint=tint, rows=rows)
    for group, icons in _TABLE
    for name, tint, rows in icons
}

# The groups, in the order they are declared above.
GROUPS: tuple[str, ...] = tuple(group for group, _ in _TABLE)


def resolve(name: str, tint: str | None = None) -> list[str]:
    """Return one icon's rows as pixel codes, or say why it cannot be drawn.

    The single way in, so that everything asking for an icon refuses the same
    things in the same words.
    """
    icon = ICONS.get(name)
    if icon is None:
        raise UnknownIcon(
            "there is no icon named %r; ask the service for the list at "
            "/enumerations/icons" % name
        )
    return icon.pixels(tint)
