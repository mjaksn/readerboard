"""Pin the shape of every built-in icon.

The icon table is data, and data fails quietly. A row one dot short, a letter
that is not an ink, a name too long to sit in a tag, two icons sharing a name:
none of those raises anything when the module is imported, and every one of them
reaches the sign as a picture nobody meant. So this checks all 148 rather than
sampling, and it checks the things a reader of the table would never notice.

What it does not check is whether an icon looks like what it is called. That
took four sessions in front of the sign and cannot be done here.
"""

import re

import pytest

from readerboard import icons
from readerboard.protocol import constants as c
from readerboard.protocol import frames
from readerboard.protocol.tokens import MARKUP_BY_TEXT

ALL_ICONS = sorted(icons.ICONS.values(), key=lambda icon: icon.name)


def ids(icon):
    return icon.name


class TestEveryIcon:
    @pytest.mark.parametrize("icon", ALL_ICONS, ids=ids)
    def test_it_is_seven_rows(self, icon):
        # The display is seven dots high, and a taller picture loses its lower
        # rows on the sign without saying so.
        assert len(icon.rows) == icons.ICON_HEIGHT

    @pytest.mark.parametrize("icon", ALL_ICONS, ids=ids)
    def test_its_rows_are_all_the_same_width(self, icon):
        assert len({len(row) for row in icon.rows}) == 1

    @pytest.mark.parametrize("icon", ALL_ICONS, ids=ids)
    def test_it_fits_the_picture_file_it_would_be_written_into(self, icon):
        # A picture wider than its allocation is not refused and not clipped.
        # It draws damaged.
        assert 1 <= icon.width <= icons.ICON_MAX_WIDTH

    @pytest.mark.parametrize("icon", ALL_ICONS, ids=ids)
    def test_every_dot_is_an_ink_or_the_marker(self, icon):
        stray = {dot for row in icon.rows for dot in row} - set(icons.INK_CODES) - {icons.INK_MARKER}
        assert not stray

    @pytest.mark.parametrize("icon", ALL_ICONS, ids=ids)
    def test_the_marker_appears_only_where_a_tint_can_fill_it(self, icon):
        # An icon with no tint has no ink to put in the marker's place, so a
        # marker in one would be drawn as whatever the default happened to be.
        marked = any(icons.INK_MARKER in row for row in icon.rows)
        assert marked == icon.tintable

    @pytest.mark.parametrize("icon", ALL_ICONS, ids=ids)
    def test_its_name_can_sit_inside_a_tag(self, icon):
        assert re.match(icons.ICON_NAME_PATTERN, icon.name)

    @pytest.mark.parametrize("icon", ALL_ICONS, ids=ids)
    def test_it_renders_to_pixel_codes_the_sign_has(self, icon):
        drawn = "".join(icon.pixels())
        assert set(drawn.encode("ascii")) <= set(c.DOTS_PIXEL_CODES)

    @pytest.mark.parametrize("icon", ALL_ICONS, ids=ids)
    def test_what_it_renders_to_is_a_picture_the_frame_builder_accepts(self, icon):
        # The two halves have to agree about what a row is, and this is the
        # only place that puts them together.
        assert frames.write_dots_file(b"6", icon.pixels()).startswith(
            b"I6%02X%02X" % (icons.ICON_HEIGHT, icon.width)
        )

    @pytest.mark.parametrize("icon", ALL_ICONS, ids=ids)
    def test_it_is_not_blank(self, icon):
        # An icon of nothing but unlit dots would draw as a gap, and a gap is
        # not something anybody asks for by name.
        assert any(dot != "." for row in icon.rows for dot in row)


class TestTheSetAsAWhole:
    def test_no_two_icons_share_a_name(self):
        # The table is a tuple of tuples, so a duplicate would silently be
        # dropped when ICONS is built and the loser would never be drawable.
        declared = [name for _, group in icons._TABLE for name, _, _ in group]
        assert len(declared) == len(set(declared))

    def test_every_icon_belongs_to_a_declared_group(self):
        assert {icon.group for icon in ALL_ICONS} == set(icons.GROUPS)

    def test_no_group_is_empty(self):
        for group in icons.GROUPS:
            assert any(icon.group == group for icon in ALL_ICONS)

    def test_the_default_ink_of_a_tintable_icon_is_a_real_ink(self):
        for icon in ALL_ICONS:
            if icon.tintable:
                assert icon.tint in icons.TINT_INKS.values()

    def test_the_tint_words_are_the_colour_tokens_own(self):
        # A message that can say <red> should not have to learn a second
        # spelling to say <icon:check:red>. This is what stops the two drifting.
        for word in icons.TINT_INKS:
            assert "<%s>" % word in MARKUP_BY_TEXT

    def test_every_ink_has_a_pixel_code_the_sign_draws(self):
        assert set(icons.INK_CODES.values()) <= set(c.DOTS_PIXEL_CODES.decode("ascii"))

    def test_the_marker_is_not_itself_an_ink(self):
        assert icons.INK_MARKER not in icons.INK_CODES


class TestResolving:
    def test_an_icon_draws_in_its_own_ink_by_default(self):
        assert icons.resolve("check") == icons.ICONS["check"].pixels()

    def test_a_tint_replaces_the_ink_and_leaves_the_unlit_dots_alone(self):
        green = icons.resolve("check", "green")
        red = icons.resolve("check", "red")
        assert green != red
        assert [row.replace("2", "1") for row in green] == red

    def test_a_fixed_colour_icon_keeps_its_colours(self):
        # The sun is amber and yellow. A green one is not a sun.
        assert icons.resolve("sun") == icons.ICONS["sun"].pixels()

    def test_asking_for_an_unknown_icon_says_where_the_list_is(self):
        with pytest.raises(icons.UnknownIcon, match="enumerations/icons"):
            icons.resolve("teapot")

    def test_tinting_a_fixed_colour_icon_is_refused_rather_than_ignored(self):
        with pytest.raises(icons.UntintableIcon, match="takes no tint"):
            icons.resolve("sun", "green")

    def test_an_unknown_tint_names_the_ones_there_are(self):
        with pytest.raises(icons.UnknownTint, match="dimgreen"):
            icons.resolve("check", "chartreuse")
