"""Walks a markdown-it-py SyntaxTreeNode tree into a Gtk.TextBuffer.

Every block-level emission also appends a PrintItem to `self.print_model`,
built in the same pass -- this is the fix for the fact that
Gtk.TextChildAnchor-embedded tables (and separators) are invisible to any
attempt to reconstruct print content by re-walking the buffer afterward.
Printing (see printing.py) works off print_model exclusively, never off
the live buffer. For the same reason, `self.tables` records each table's
anchor and cell labels so findbar.py can search that text too.
"""
import re

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk

from . import tags as tagdefs
from . import tables
from . import highlighting
from . import images as imagelib


def slugify_heading(text):
    """GitHub-compatible heading anchor slug: lowercase, drop anything
    that isn't a word character, space or hyphen, then turn spaces into
    hyphens. Matches what GitHub puts in a heading's `id` and what a
    `[text](#slug)` link is written against.
    """
    slug = re.sub(r"[^\w\s-]", "", text.strip().lower())
    return re.sub(r"\s+", "-", slug)

# Inline node types that just wrap their children in one more tag, with no
# other behavior -- driven by a table instead of three near-identical
# elif branches in _walk_inline.
_SIMPLE_INLINE_WRAP_TAGS = {"strong": "strong", "em": "em", "s": "strike"}


def _task_checkbox_state(html_inline_content):
    """Recognize mdit_py_plugins.tasklists' injected checkbox markup (an
    `<input class="task-list-item-checkbox" ...>` html_inline token) and
    return True/False for checked/unchecked, or None if this html_inline
    isn't one -- the one place that knows what that plugin's markup looks
    like, rather than an inline sniff repeated wherever it's needed."""
    if "task-list-item-checkbox" not in html_inline_content:
        return None
    return "checked" in html_inline_content


def _without_trailing_newline(runs):
    """A copy of `runs` with one trailing "\\n" removed from the last run
    (dropping that run entirely if nothing else is left)."""
    if not runs or not runs[-1][0].endswith("\n"):
        return list(runs)
    text, tags = runs[-1]
    trimmed = text[:-1]
    return list(runs[:-1]) + ([(trimmed, tags)] if trimmed else [])


class PrintItem:
    __slots__ = ("kind", "runs", "block_tags", "rows", "language", "image", "gap", "diagram")

    def __init__(self, kind, runs=None, block_tags=None, rows=None, language=None,
                 image=None, diagram=None):
        self.kind = kind
        self.runs = runs or []
        self.block_tags = block_tags or []
        self.rows = rows
        self.language = language
        # Space above this block, in the same screen pixels the buffer's
        # block-gap tags use, filled in by the walker once the collapsed
        # margin is known. Printing scales it to points rather than
        # inventing a spacing model of its own -- that is what used to
        # make printed lists and headings space differently from the
        # screen.
        self.gap = 0
        # For "image": the live images.ImageView. Printing reads its
        # texture at print time rather than at render time, so an image
        # the reader loaded after opening the document still prints.
        self.image = image
        # For "diagram": the parsed mermaid model, *not* the on-screen
        # scene. Paper and screen lay a diagram out with different fonts
        # (see printing._base_font), and a scene is only valid for the
        # font it was measured with, so print re-lays it out from the
        # model rather than scaling the screen's geometry.
        self.diagram = diagram


class RenderCtx:
    __slots__ = ("block_tags",)

    def __init__(self, block_tags=None):
        self.block_tags = block_tags or []

    def push_block(self, tag_name):
        return RenderCtx(self.block_tags + [tag_name])


class MarkdownRenderer:
    """Stateful per-render helper -- create one, call render() once, read
    print_model/dispatch_targets/footnote marks off it afterward. A fresh
    instance is used for every reload so nothing leaks between renders.
    """

    _BLOCK_TOP_MARGIN = {"heading": 24, "hr": 24, "footnote_block": 24}
    _BLOCK_BOTTOM_MARGIN = {
        "paragraph": 16,
        "bullet_list": 16,
        "ordered_list": 16,
        "blockquote": 16,
        "fence": 16,
        "table": 16,
        "heading": 16,
        "hr": 24,
        "footnote_block": 16,
    }
    # Padding, not margin: room a block needs for chrome painted *outside*
    # its own text box -- decorated_textview.py's fenced-code panel. It is
    # added to the neighbouring gap rather than collapsed into it, because
    # a collapsed margin would put the neighbour's text where the panel is
    # about to be drawn. Kept out of the code-block tag's own
    # pixels-above/below-lines deliberately: those apply to *every* line
    # of the fence, not just its first and last.
    _BLOCK_PADDING = {"fence": tagdefs.CODE_BLOCK_PADDING}
    # Blocks that only ever contain other blocks. Their last child already
    # recorded whatever trailing padding is owed, so re-deriving it from
    # the container's own type would throw that away.
    _CONTAINER_BLOCKS = {"bullet_list", "ordered_list", "blockquote", "footnote_block"}
    _LIST_BLOCKS = {"bullet_list", "ordered_list"}
    # GitHub's `li > p { margin-top: 16px }` -- the same 1em everything else
    # gets, which is the point of writing it this way rather than as its own
    # number. See _list_item_top_margin.
    _LOOSE_ITEM_TOP_MARGIN = _BLOCK_BOTTOM_MARGIN["paragraph"]

    def __init__(self):
        self.tag_table = None
        self.dispatch_targets = {}      # tag name -> {"type": ..., ...}
        self.print_model = []
        # Gtk.Labels (table cells) whose markup contains a clickable
        # link -- these live outside the TextBuffer entirely, so they
        # can't go through dispatch_targets/target_at_iter like buffer
        # links do. The caller (window.py) connects "activate-link" on
        # each after render() returns.
        self.table_link_labels = []
        # (anchor, tables.TableCell grid shaped [row][col]) per table --
        # findbar.py uses this to search and highlight table-cell text,
        # which never enters the TextBuffer.
        self.tables = []
        # images.ImageView per rendered image, for window.py to width-sync
        # and (for http(s) ones) to load on demand.
        self.images = []
        # mermaid.DiagramView per drawn diagram. Width-synced and zoomed
        # by window.py the same way images are.
        self.diagrams = []
        # Gtk.TextChildAnchor -> {"kind": "hr"/"image"/"table"/"diagram",
        # ...}. clipboard.py's buffer walk sees a bare anchor object (the
        # buffer's text stream holds only its object-replacement
        # character), and needs this to know what stood there -- table
        # and diagram anchors are recorded too, so it can skip them with
        # a documented reason rather than mishandling them.
        self.anchor_descriptors = {}
        self._footnote_ref_marks = {}   # label -> mark name
        self._footnote_def_marks = {}   # label -> mark name
        self._heading_marks = {}        # slug -> mark name
        self._heading_slug_counts = {}  # slug -> number of headings seen with it
        self._pending_anchors = []      # (anchor, widget) drained by caller
        self._instance_counter = 0
        # Set by a block that needs different outside-the-text-box padding
        # from its node type's default, read and cleared by
        # _walk_block_node right after dispatch.
        self._block_padding_override = None
        self._prev_block_bottom = None
        self._prev_block_padding = 0
        self._link_color = tagdefs.link_color_hex(dark=False)  # overwritten by render()
        self._base_dir = None                                  # ditto

    # -- public API ---------------------------------------------------

    def render(self, tree, buffer, dark=False, base_dir=None):
        self.tag_table = buffer.get_tag_table()
        self._prev_block_bottom = None
        self._prev_block_padding = 0
        self._link_color = tagdefs.link_color_hex(dark)
        # Directory the document lives in, for resolving relative image
        # paths -- the same base window.py resolves relative links against.
        self._base_dir = base_dir
        it = buffer.get_start_iter()
        # "prose" carries only pixels-inside-wrap (see tags.py) -- seeding
        # it into the root context means every push_block() descendant
        # (blockquotes, list items, nested lists...) inherits it too,
        # since push_block only ever appends, never replaces.
        self._walk_block(tree, buffer, it, RenderCtx(["prose"]))

    def attach_pending_widgets(self, textview):
        """Must be called after render() and after the buffer is assigned
        to the view -- Gtk.TextView.add_child_at_anchor requires a realized
        text view/buffer pairing that create_child_anchor doesn't. Returns
        the attached widgets that need to track the view's content width
        (hr separators, table frames) -- see window.py's width-sync
        handler for why: Gtk.TextView never stretches anchored children to
        fill the line the way hexpand does in an ordinary container, no
        matter what's set on the widget itself, so something external has
        to actively push a width onto them.

        Images are excluded: they want the available width as a *ceiling*
        to scale down to, not as a size to fill, so window.py drives them
        through ImageView.set_available_width instead."""
        fill_width_widgets = [
            widget for _anchor, widget in self._pending_anchors
            if not isinstance(widget, imagelib.ImageView)
        ]
        for anchor, widget in self._pending_anchors:
            textview.add_child_at_anchor(widget, anchor)
        self._pending_anchors = []
        return fill_width_widgets

    def footnote_def_mark_name(self, label):
        return self._footnote_def_marks.get(label)

    def footnote_ref_mark_name(self, label):
        return self._footnote_ref_marks.get(label)

    def heading_mark_name(self, slug):
        return self._heading_marks.get(slug)

    def target_at_iter(self, it):
        """Resolve the dispatch target (link/footnote) at a buffer
        position, or None. Window.py's click and hover handlers both call
        this instead of each re-scanning the iter's tags themselves."""
        for tag in it.get_tags():
            target = self.dispatch_targets.get(tag.get_property("name") or "")
            if target is not None:
                return target
        return None

    # -- instance-tag bookkeeping --------------------------------------

    def _new_instance_tag(self, prefix):
        self._instance_counter += 1
        name = f"{prefix}-{self._instance_counter}"
        tagdefs.ensure_instance_tag(self.tag_table, name)
        return name

    def _emit(self, buffer, it, text, buffer_tags, run_tags, runs):
        if not text:
            return
        buffer.insert_with_tags_by_name(it, text, *buffer_tags)
        runs.append((text, run_tags))

    # -- block-level walk ------------------------------------------------

    def _walk_block(self, node, buffer, it, ctx):
        for child in node.children:
            self._walk_block_node(child, buffer, it, ctx)

    def _walk_block_node(self, child, buffer, it, ctx):
        """Dispatch one block and apply collapsed-style inter-block spacing.
        Gap before B is max(bottom(A), top(B)) -- plus any padding either
        side needs for chrome drawn outside its text box, which doesn't
        collapse (see _BLOCK_PADDING). bottom(B) then carries to the next
        block.
        """
        t = child.type
        # Both of these have to be read *before* dispatching: walking a
        # container block runs its children through here too, leaving
        # whatever the last of them owed.
        prev_bottom = self._prev_block_bottom
        prev_padding = self._prev_block_padding
        start_mark = buffer.create_mark(None, it, True) if prev_bottom is not None else None
        first_item = len(self.print_model)
        self._dispatch_block_node(t, child, buffer, it, ctx)
        # A fence normally reserves room for the code panel painted around
        # it; one drawn as a mermaid diagram has no panel, and says so by
        # setting the override (see _emit_diagram).
        padding = self._BLOCK_PADDING.get(t, 0)
        if self._block_padding_override is not None:
            padding = self._block_padding_override
            self._block_padding_override = None
        if start_mark is not None:
            gap = max(prev_bottom, self._BLOCK_TOP_MARGIN.get(t, 0))
            gap += prev_padding + padding
            self._apply_gap(buffer, start_mark, gap)
            self._record_print_gap(first_item, gap)
        self._prev_block_bottom = self._block_bottom_margin(child)
        if t not in self._CONTAINER_BLOCKS:
            self._prev_block_padding = padding

    def _dispatch_block_node(self, t, child, buffer, it, ctx):
        if t == "heading":
            level = int(child.tag[1])
            self._register_heading_anchor(child, buffer, it)
            self._emit_simple_paragraph(child, buffer, it, ctx, extra_tag=f"heading{level}")
        elif t == "paragraph":
            self._emit_simple_paragraph(child, buffer, it, ctx)
        elif t == "blockquote":
            self._walk_block(child, buffer, it, ctx.push_block("blockquote"))
        elif t in ("bullet_list", "ordered_list"):
            self._walk_list(child, buffer, it, ctx, ordered=(t == "ordered_list"))
        elif t == "fence":
            self._emit_code_block(child, buffer, it, ctx)
        elif t == "hr":
            self._emit_hr(buffer, it, ctx)
        elif t == "table":
            self._emit_table(child, buffer, it, ctx)
        elif t == "footnote_block":
            self._walk_footnote_block(child, buffer, it, ctx)
        # Anything else (raw html_block, etc.) is silently skipped -- v1
        # scope cut, not requested.

    @classmethod
    def _block_bottom_margin(cls, node):
        """The space `node` owes below itself, before collapsing.

        Two blocks owe nothing despite their entry in the table above,
        each matching a rule GitHub spells out:

        A paragraph in a **tight** list is not really a paragraph. A list
        is tight when no blank line separates its items; markdown-it emits
        no <p> for those items and flags their paragraphs `hidden`, and no
        <p> means no box to carry a margin.

        A **nested** list owes nothing either (`ul ul { margin-top: 0;
        margin-bottom: 0 }`), which leaves the gap after one to the
        following item's own top margin -- see _list_item_top_margin.
        """
        if node.type == "paragraph" and node.hidden:
            return 0
        if cls._is_nested_list(node):
            return 0
        return cls._BLOCK_BOTTOM_MARGIN.get(node.type, 0)

    @classmethod
    def _is_nested_list(cls, node):
        """A list that is one of a list item's own blocks rather than a
        top-level one -- GitHub's `ul ul, ul ol, ol ol, ol ul` selector."""
        return (node.type in cls._LIST_BLOCKS
                and node.parent is not None and node.parent.type == "list_item")

    @classmethod
    def _list_item_top_margin(cls, item):
        """What a list item asks for above itself.

        A *loose* item's paragraph carries a top margin of its own
        (GitHub's `li > p`), which matters in one place: an item following
        one that ended in a nested list. That list owes nothing below
        itself, so with only the previous block's margin to go on the
        following item pulls up to the bare item gap -- 1.75em against the
        browser's 2.5em. A tight item has no paragraph to carry a margin
        and asks only for li + li.
        """
        for block in item.children:
            if block.type == "paragraph":
                return tagdefs.LIST_ITEM_GAP if block.hidden else cls._LOOSE_ITEM_TOP_MARGIN
        return tagdefs.LIST_ITEM_GAP

    def _record_print_gap(self, first_item, gap):
        """Give the print item a block's spacing wound up on -- the first
        one it emitted, matching the buffer line the gap tag went on. A
        block that emitted nothing to print (an empty table) has none."""
        if first_item < len(self.print_model):
            self.print_model[first_item].gap = gap

    @staticmethod
    def _tag_first_line(buffer, start_mark, tag_name, delete_mark=True):
        """Apply a paragraph-level tag to just the first buffer line at
        `start_mark` -- the only way to give a block's opening line
        spacing or an indent of its own, since Gtk.TextTag properties like
        pixels-above-lines and indent apply to every line they cover."""
        start_iter = buffer.get_iter_at_mark(start_mark)
        end_iter = start_iter.copy()
        if not end_iter.ends_line():
            end_iter.forward_to_line_end()
        buffer.apply_tag_by_name(tag_name, start_iter, end_iter)
        if delete_mark:
            buffer.delete_mark(start_mark)

    @classmethod
    def _apply_gap(cls, buffer, start_mark, gap):
        """Apply top spacing to just the first buffer line at `start_mark`."""
        if isinstance(gap, str):
            tag_name = gap
        else:
            tag = tagdefs.ensure_block_gap_tag(buffer.get_tag_table(), gap)
            tag_name = tag.get_property("name")
        cls._tag_first_line(buffer, start_mark, tag_name)

    def _emit_simple_paragraph(self, node, buffer, it, ctx, extra_tag=None):
        inline_tags = [extra_tag] if extra_tag else []
        self._emit_paragraph_body(node, buffer, it, ctx, runs=[], leading_inline_tags=inline_tags)

    def _emit_paragraph_body(self, para_node, buffer, it, ctx, runs, leading_inline_tags=()):
        """Walk a paragraph's inline content (plus any trailing footnote
        back-reference arrow) into `buffer`/`runs`, terminate the line, and
        record the PrintItem. `runs` may already hold a leading marker run
        (list bullet/number, footnote-def label) inserted by the caller --
        shared by _emit_simple_paragraph and _walk_block_with_marker so
        this sequence exists in exactly one place."""
        if para_node.children:
            self._walk_inline(para_node.children[0], buffer, it, ctx.block_tags, list(leading_inline_tags), runs)
            anchor = self._paragraph_footnote_anchor(para_node)
            if anchor is not None:
                self._emit_footnote_back(anchor, buffer, it, ctx.block_tags, [], runs)
        buffer.insert(it, "\n")
        self.print_model.append(PrintItem("paragraph", runs=runs, block_tags=list(ctx.block_tags)))

    @staticmethod
    def _paragraph_footnote_anchor(para_node):
        """footnote_anchor (the back-to-reference arrow) is emitted by
        mdit_py_plugins as a direct child of the paragraph, a sibling of
        the paragraph's `inline` node -- not nested inside it."""
        for extra in para_node.children[1:]:
            if extra.type == "footnote_anchor":
                return extra
        return None

    def _emit_code_block(self, node, buffer, it, ctx):
        info = (node.info or "").strip()
        language = info.split()[0] if info else None
        code = node.content
        if language and language.lower() == "mermaid" and self._emit_diagram(code, buffer, it, ctx):
            return
        runs = []
        for text, pyg_tag in highlighting.highlight_runs(code, language):
            run_tags = ["code-block"] + ([pyg_tag] if pyg_tag else [])
            buffer.insert_with_tags_by_name(it, text, *(ctx.block_tags + run_tags))
            runs.append((text, run_tags))
        if not code.endswith("\n"):
            buffer.insert(it, "\n")
        # The buffer needs that final newline to end the block's last
        # line; the print layout must not have it, or Pango adds an empty
        # line whose height the code panel then pads around. Paragraphs
        # already keep their terminator out of `runs` for the same reason.
        self.print_model.append(
            PrintItem("code-block", runs=_without_trailing_newline(runs),
                      block_tags=list(ctx.block_tags), language=language)
        )

    def _emit_diagram(self, code, buffer, it, ctx):
        """Draw a ```mermaid fence as a diagram. False means "couldn't",
        and the caller emits the fence as an ordinary code block.

        The import is deferred rather than made at module level for the
        same reason document.py defers markdown-it: a document with no
        diagrams in it shouldn't pay to import cairo and PangoCairo, and
        most documents have none.
        """
        from . import mermaid
        try:
            diagram = mermaid.parse(code)
            scene = mermaid.build_scene(diagram, mermaid.ui_font())
        except mermaid.Unsupported:
            return False
        except Exception:
            # A viewer must not fail to open a document because one
            # diagram in it tripped over a bug in this layout code. The
            # code-block fallback still shows the reader the source.
            return False
        view = mermaid.DiagramView(scene, mermaid.ui_font())
        # No code panel is painted around a diagram, so it doesn't need
        # the room a fence otherwise reserves for one.
        self._block_padding_override = 0
        anchor = buffer.create_child_anchor(it)
        self._pending_anchors.append((anchor, view))
        # `view` itself, not just "kind" -- clipboard.py rasterizes its
        # already-built `.scene` to a PNG data URI, the same reasoning
        # as reading a loaded image's live texture at copy time.
        self.anchor_descriptors[anchor] = {"kind": "diagram", "view": view}
        self.diagrams.append(view)
        buffer.insert(it, "\n")
        self.print_model.append(
            PrintItem("diagram", diagram=diagram, block_tags=list(ctx.block_tags))
        )
        return True

    def _emit_hr(self, buffer, it, ctx):
        anchor = buffer.create_child_anchor(it)
        separator = Gtk.Separator(hexpand=True)
        self._pending_anchors.append((anchor, separator))
        self.anchor_descriptors[anchor] = {"kind": "hr"}
        buffer.insert(it, "\n")
        self.print_model.append(PrintItem("hr", block_tags=list(ctx.block_tags)))

    def _emit_table(self, node, buffer, it, ctx):
        widget, rows, link_labels, cells = tables.build_table_widget(node, self._link_color)
        self.table_link_labels.extend(link_labels)
        anchor = buffer.create_child_anchor(it)
        self.tables.append((anchor, cells))
        self._pending_anchors.append((anchor, widget))
        # `rows` is the same plain-text grid print_model gets below --
        # clipboard.py reconstructs a real <table>/GFM pipe table from
        # it when a selection spans this anchor, rather than the cell
        # markup each cell's own separately-copyable Gtk.Label carries
        # (see tables.py's build_table_widget docstring).
        self.anchor_descriptors[anchor] = {"kind": "table", "rows": rows}
        buffer.insert(it, "\n")
        self.print_model.append(PrintItem("table", rows=rows, block_tags=list(ctx.block_tags)))

    def _emit_image(self, node, buffer, it, block_tags):
        """Images are inline nodes, so the anchor goes wherever the image
        sat in the text -- an image alone in its paragraph reads as a
        block, one mid-sentence stays in the line, and Gtk.TextView's own
        line flow handles both without either case being special-cased.

        `alt` comes from flattening the node's children, not from the
        `alt` attribute (markdown-it leaves that empty) and not from
        `content` either -- content is the *raw* alt source, so
        `![a *b*](x)` would show its asterisks.

        Known print limitation: the image's PrintItem lands in
        print_model as its own block, appended the moment the inline walk
        reaches it -- but a paragraph's own PrintItem isn't appended until
        the whole paragraph has been walked. So an image *inside* a
        paragraph prints above that paragraph rather than within it.
        Harmless for the overwhelmingly common "image alone in its own
        paragraph" case, which is why it's left alone: fixing it means
        splitting a paragraph's runs around the image and emitting several
        PrintItems per paragraph. On screen the ordering is always right,
        since there the anchor sits in the text flow itself.
        """
        src = node.attrs.get("src", "")
        alt = tables.inline_plain_text(node)
        view = imagelib.ImageView(src, alt, self._base_dir)
        anchor = buffer.create_child_anchor(it)
        self.images.append(view)
        self._pending_anchors.append((anchor, view))
        # `view` itself, not just src/alt: clipboard.py reads its
        # `.texture` at copy time (same reasoning as printing.py reading
        # it at print time -- an image loaded, or a remote one fetched,
        # after this render still has to be reflected live).
        self.anchor_descriptors[anchor] = {"kind": "image", "src": src, "alt": alt, "view": view}
        self.print_model.append(
            PrintItem("image", block_tags=list(block_tags), image=view)
        )

    # -- lists / task lists -----------------------------------------------

    def _walk_list(self, node, buffer, it, ctx, ordered):
        # Both names count: a list nested inside an item's continuation
        # blocks sees its ancestor as a "list-body-" tag, and missing it
        # would restart the nesting at level 0.
        level = sum(
            1 for t in ctx.block_tags
            if t.startswith("list-indent-") or t.startswith("list-body-")
        )
        indent_tag = tagdefs.ensure_list_indent_tag(self.tag_table, level)
        child_ctx = ctx.push_block(indent_tag.get_property("name"))
        for index, item in enumerate(node.children):
            # The list's first item gets its "space above" from the
            # enclosing block-gap wrap in _walk_block_node instead (this
            # whole list is itself the top-level block that gap applies
            # to) -- every item after that needs its own, smaller gap,
            # since items were previously packed with zero separation.
            needs_gap = index > 0
            start_mark = buffer.create_mark(None, it, True) if needs_gap else None
            padding = self._prev_block_padding
            # The ordinary collapsed rule, one level down: what the item
            # above owes below itself against what this one asks for above.
            # Both sides carry the tight/loose difference.
            prev_bottom = self._prev_block_bottom or 0
            item_top = self._list_item_top_margin(item)
            first_item = len(self.print_model)
            self._walk_list_item(item, buffer, it, child_ctx, ordered)
            if start_mark is not None:
                gap = max(prev_bottom, item_top) + padding
                # The plain case reuses the static tag rather than minting
                # an identical one per document.
                is_plain = gap == tagdefs.LIST_ITEM_GAP
                self._apply_gap(buffer, start_mark, "list-item-gap" if is_plain else gap)
                self._record_print_gap(first_item, gap)

    def _walk_list_item(self, item, buffer, it, ctx, ordered):
        is_task = bool(item.attrs) and "task-list-item" in str(item.attrs.get("class", ""))
        block_children = list(item.children)
        if is_task:
            checked = self._detect_task_checked(block_children)
            marker_text = "☑" if checked else "☐"  # ☑ / ☐
            marker_tag = "task-checked-glyph" if checked else "task-unchecked-glyph"
        elif ordered:
            marker_text = f"{item.info}. "
            marker_tag = "list-marker"
        else:
            marker_text = "•  "  # •
            marker_tag = "list-marker"
        self._walk_block_with_marker(
            block_children, buffer, it, ctx, marker_text, marker_tag,
            hang=True, body_ctx=self._list_body_ctx(ctx),
        )

    def _list_body_ctx(self, ctx):
        """`ctx` with this list level's marker column swapped for its text
        column, for the blocks of an item that follow the one carrying the
        marker -- they line up under the item's text, not under its
        bullet."""
        for i in range(len(ctx.block_tags) - 1, -1, -1):
            name = ctx.block_tags[i]
            if not name.startswith("list-indent-"):
                continue
            level = int(name.rsplit("-", 1)[1])
            body = tagdefs.ensure_list_body_tag(self.tag_table, level)
            tags = list(ctx.block_tags)
            tags[i] = body.get_property("name")
            return RenderCtx(tags)
        return ctx

    def _detect_task_checked(self, block_children):
        if not block_children or block_children[0].type != "paragraph":
            return False
        para = block_children[0]
        if not para.children:
            return False
        for child in para.children[0].children:
            if child.type == "html_inline":
                state = _task_checkbox_state(child.content)
                if state is not None:
                    return state
        return False

    def _walk_block_with_marker(self, block_children, buffer, it, ctx, marker_text,
                                marker_tag, hang=False, body_ctx=None):
        """Shared by list items and footnote definitions: render `marker_text`
        immediately before the first paragraph's content (same visual line),
        then walk any remaining block children normally.

        `hang` gives that one line the hanging indent that puts the marker
        left of the item's text (list items; footnote definitions have no
        indent to hang out of), and `body_ctx` is the context the
        remaining blocks are walked in -- the item's text column rather
        than its marker column."""
        hang_mark = buffer.create_mark(None, it, True) if hang else None
        if block_children and block_children[0].type == "paragraph":
            first, rest = block_children[0], block_children[1:]
            runs = [(marker_text, [marker_tag])]
            buffer.insert_with_tags_by_name(it, marker_text, *(ctx.block_tags + [marker_tag]))
            self._emit_paragraph_body(first, buffer, it, ctx, runs)
            self._prev_block_bottom = self._block_bottom_margin(first)
            self._prev_block_padding = 0
        else:
            # The trailing "\n" is inserted bare (no tags), matching
            # every other block terminator (_emit_paragraph_body,
            # _emit_hr, ...) -- an invisible character's styling can't
            # affect what's drawn, but clipboard.py's buffer walk uses
            # "an untagged newline" as its one signal for "a block ends
            # here", and a tagged one here would hide that boundary.
            buffer.insert_with_tags_by_name(it, marker_text, *(ctx.block_tags + [marker_tag]))
            buffer.insert(it, "\n")
            self.print_model.append(
                PrintItem("paragraph", runs=[(marker_text, [marker_tag])], block_tags=list(ctx.block_tags))
            )
            rest = block_children
            # A bare marker on a line of its own -- an item that opens with
            # a fence or a nested list, so markdown-it emitted no paragraph
            # to consult. There is no paragraph here to owe a paragraph's
            # margin, and whatever follows brings its own top margin.
            self._prev_block_bottom = 0
            self._prev_block_padding = 0
        if hang_mark is not None:
            self._tag_first_line(buffer, hang_mark, "list-hang")
        for child in rest:
            self._walk_block_node(child, buffer, it, body_ctx or ctx)

    # -- footnotes -------------------------------------------------------

    def _register_heading_anchor(self, node, buffer, it):
        """Record a buffer mark at this heading so `[text](#slug)` links
        elsewhere in the document can scroll to it -- see window.py's
        `_open_href`. A repeated slug gets GitHub's own "-1", "-2", ...
        suffix, so only the first heading with a given title is ever the
        bare `#slug` target, exactly as on GitHub.
        """
        text = tables.inline_plain_text(node)
        slug = slugify_heading(text) if text else ""
        if not slug:
            return
        count = self._heading_slug_counts.get(slug, 0)
        self._heading_slug_counts[slug] = count + 1
        if count:
            slug = f"{slug}-{count}"
        self._instance_counter += 1
        mark_name = f"heading-anchor-{self._instance_counter}"
        buffer.create_mark(mark_name, it, True)
        self._heading_marks[slug] = mark_name

    def _walk_footnote_block(self, node, buffer, it, ctx):
        if not node.children:
            return
        self._emit_hr(buffer, it, ctx)
        for footnote in node.children:
            label = str(footnote.meta.get("label", ""))
            mark_name = f"footnote-def-{label}"
            if buffer.get_mark(mark_name) is None:
                buffer.create_mark(mark_name, it, True)
            self._footnote_def_marks[label] = mark_name
            self._walk_block_with_marker(list(footnote.children), buffer, it, ctx, f"{label}. ", "list-marker")

    def _emit_footnote_ref(self, node, buffer, it, block_tags, inline_tags, runs):
        label = str(node.meta.get("label", ""))
        mark_name = f"footnote-src-{label}"
        if buffer.get_mark(mark_name) is None:
            buffer.create_mark(mark_name, it, True)
        self._footnote_ref_marks[label] = mark_name
        tagname = self._new_instance_tag("footnote-ref")
        self.dispatch_targets[tagname] = {"type": "footnote-jump", "label": label}
        run_tags = inline_tags + ["footnote-ref", tagname]
        self._emit(buffer, it, label, block_tags + run_tags, run_tags, runs)

    def _emit_footnote_back(self, node, buffer, it, block_tags, inline_tags, runs):
        label = str(node.meta.get("label", ""))
        tagname = self._new_instance_tag("footnote-back")
        self.dispatch_targets[tagname] = {"type": "footnote-back", "label": label}
        run_tags = inline_tags + ["footnote-ref", tagname]
        self._emit(buffer, it, " ↩", block_tags + run_tags, run_tags, runs)

    # -- inline walk -------------------------------------------------------

    def _walk_inline(self, node, buffer, it, block_tags, inline_tags, runs):
        for child in node.children:
            t = child.type
            if t == "text":
                self._emit(buffer, it, child.content, block_tags + inline_tags, inline_tags, runs)
            elif t == "softbreak":
                self._emit(buffer, it, " ", block_tags + inline_tags, inline_tags, runs)
            elif t == "hardbreak":
                self._emit(buffer, it, "\n", block_tags + inline_tags, inline_tags, runs)
            elif t in _SIMPLE_INLINE_WRAP_TAGS:
                wrap_tag = _SIMPLE_INLINE_WRAP_TAGS[t]
                self._walk_inline(child, buffer, it, block_tags, inline_tags + [wrap_tag], runs)
            elif t == "code_inline":
                new_tags = inline_tags + ["code-inline"]
                self._emit(buffer, it, child.content, block_tags + new_tags, new_tags, runs)
            elif t == "link":
                tagname = self._new_instance_tag("link")
                self.dispatch_targets[tagname] = {"type": "url", "href": child.attrs.get("href", "")}
                self._walk_inline(child, buffer, it, block_tags, inline_tags + ["link", tagname], runs)
            elif t == "image":
                self._emit_image(child, buffer, it, block_tags)
            elif t == "footnote_ref":
                self._emit_footnote_ref(child, buffer, it, block_tags, inline_tags, runs)
            elif t == "footnote_anchor":
                self._emit_footnote_back(child, buffer, it, block_tags, inline_tags, runs)
            elif t == "html_inline":
                # Raw HTML isn't rendered in v1. This is also what makes the
                # task-list checkbox <input> injected by mdit_py_plugins
                # disappear cleanly -- the visible glyph comes from the
                # marker text the list-item walker inserts instead.
                pass
            else:
                content = getattr(child, "content", "")
                if content:
                    self._emit(buffer, it, content, block_tags + inline_tags, inline_tags, runs)
