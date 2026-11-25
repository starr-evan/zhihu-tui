from __future__ import annotations

import re
import threading
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import parse_qs, unquote, urlparse

from rich.style import Style
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import Footer, Label, ListItem, ListView, OptionList, Static
from textual.widgets.option_list import Option
from zhihu_cli.auth import cookie_str_to_dict, get_cookie_string
from zhihu_cli.client import ZhihuClient

from . import __version__
from .themes import THEMES, load_theme, save_theme

REPO_URL = "https://github.com/starr-evan/zhihu-tui"


def real_url(href: str) -> str:
    """还原知乎的外链跳转地址。"""
    u = urlparse(href)
    if u.netloc == "link.zhihu.com":
        target = parse_qs(u.query).get("target")
        if target:
            return unquote(target[0])
    return href


def link_style(url: str, base: Style | None = None) -> Style:
    st = Style(underline=True, link=url, meta={"@click": f"app.open_url({url!r})"})
    return base + st if base else st


class _Rich(HTMLParser):
    """把知乎 HTML 渲染成带样式的 rich Text。"""

    BLOCK = {"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "figure", "ul", "ol", "table", "section"}
    HEADING = {"h1", "h2", "h3", "h4", "h5", "h6"}
    INLINE = {
        "b": Style(bold=True),
        "strong": Style(bold=True),
        "i": Style(italic=True),
        "em": Style(italic=True),
        "u": Style(underline=True),
        "s": Style(strike=True),
        "del": Style(strike=True),
        "a": Style(underline=True),
        "code": Style(bold=True),
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.text = Text()
        self.stack: list[tuple[str, Style]] = []
        self.skip = 0
        self.bq = 0
        self.pre = 0
        self.span_math: list[bool] = []
        self.video_a: list[bool] = []
        self.img_n = 0

    def _style(self) -> Style | None:
        return Style.combine([st for _, st in self.stack]) if self.stack else None

    def _trailing_nl(self) -> int:
        plain = self.text.plain
        return len(plain) - len(plain.rstrip("\n"))

    def _nl(self, n: int) -> None:
        if not self.text.plain:
            return
        need = n - self._trailing_nl()
        if need > 0:
            self.text.append("\n" * need)
            if self.bq:
                self.text.append("▎ ", style="dim")

    def _w(self, s: str, style: Style | None = None) -> None:
        if s:
            self.text.append(s, style=style if style is not None else self._style())

    def _push(self, tag: str, st: Style) -> None:
        self.stack.append((tag, st))

    def _pop(self, tag: str) -> None:
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i]
                return

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = a.get("class") or ""
        if tag in ("script", "style", "noscript"):
            self.skip += 1
            return
        if tag == "span":
            is_math = bool(a.get("data-tex"))
            self.span_math.append(is_math)
            if is_math and not self.skip:
                self._w(f"「{a['data-tex']}」", Style(italic=True))
            if is_math:
                self.skip += 1
            return
        if tag == "a" and ("video-box" in cls or "data-video-id" in a):
            if not self.skip:
                self._nl(2)
                href = real_url(a.get("href") or "")
                self._w("▶ [视频]", link_style(href, Style(dim=True)) if href else Style(dim=True))
                self._nl(2)
            self.skip += 1
            self.video_a.append(True)
            return
        if tag == "a":
            self.video_a.append(False)
        if self.skip:
            return
        if tag == "a" and a.get("href"):
            self._push(tag, link_style(real_url(a["href"])))
            return
        if tag in self.HEADING:
            self._nl(2)
            self._push(tag, Style(bold=True))
        elif tag in self.BLOCK:
            self._nl(2)
        elif tag == "br":
            self._w("\n")
            if self.bq:
                self._w("▎ ", Style(dim=True))
        elif tag == "li":
            self._nl(1)
            self._w("• ")
        elif tag == "blockquote":
            self._nl(2)
            self.bq += 1
            self._w("▎ ", Style(dim=True))
            self._push(tag, Style(dim=True))
        elif tag == "hr":
            self._nl(2)
            self._w("─" * 24, Style(dim=True))
            self._nl(2)
        elif tag == "figcaption":
            self._nl(1)
            self._push(tag, Style(dim=True))
        elif tag == "pre":
            self._nl(2)
            self.pre += 1
            self._push(tag, Style(dim=True))
        elif tag == "tr":
            self._nl(1)
        elif tag in ("td", "th"):
            if self.text.plain and not self.text.plain.endswith("\n"):
                self._w(" │ ", Style(dim=True))
        elif tag == "img":
            tex = a.get("alt") if a.get("eeimg") else None
            if tex:
                self._w(f"「{tex}」", Style(italic=True))
            else:
                src = a.get("data-original") or a.get("data-actualsrc") or a.get("src") or ""
                self.img_n += 1
                self._nl(1)
                label = f"[图片 {self.img_n}]"
                self._w(label, link_style(src, Style(dim=True)) if src.startswith("http") else Style(dim=True))
                self._nl(1)
        elif tag in self.INLINE:
            self._push(tag, self.INLINE[tag])

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript"):
            self.skip = max(0, self.skip - 1)
            return
        if tag == "span":
            if self.span_math and self.span_math.pop():
                self.skip = max(0, self.skip - 1)
            return
        if tag == "a":
            if self.video_a and self.video_a.pop():
                self.skip = max(0, self.skip - 1)
                return
        if self.skip:
            return
        if tag in self.HEADING:
            self._pop(tag)
            self._nl(2)
        elif tag in self.BLOCK or tag == "li":
            self._nl(2 if tag in self.BLOCK else 1)
        elif tag == "blockquote":
            self._pop(tag)
            self.bq = max(0, self.bq - 1)
            self._nl(2)
        elif tag == "figcaption":
            self._pop(tag)
            self._nl(2)
        elif tag == "pre":
            self._pop(tag)
            self.pre = max(0, self.pre - 1)
            self._nl(2)
        elif tag in self.INLINE:
            self._pop(tag)

    def handle_data(self, data):
        if self.skip:
            return
        if not self.pre:
            data = re.sub(r"\s+", " ", data)
            if not self.text.plain or self.text.plain.endswith("\n"):
                data = data.lstrip()
        self._w(data)


def render(html: str) -> Text:
    p = _Rich()
    p.feed(html or "")
    p.close()
    t = p.text
    t.rstrip()
    return t


def plain(html: str) -> str:
    return render(html).plain


def fmt_time(ts: int) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else ""


@dataclass
class Doc:
    title: str
    author: str
    votes: int
    comments: int
    body: Text
    aid: str | None
    url: str
    created: int = 0
    updated: int = 0
    qid: str | None = None


class Api:
    def __init__(self) -> None:
        cookie = get_cookie_string()
        if not cookie:
            raise SystemExit("未登录，请先执行: zhihu login --qrcode")
        self.client = ZhihuClient(cookie_str_to_dict(cookie))
        self.lock = threading.Lock()
        try:
            self.me = self.client.get_self_info().get("name", "?")
        except Exception:
            self.me = "登录态无效"

    def hot(self) -> list[dict]:
        with self.lock:
            data = self.client.get_hot_list(limit=50).get("data", [])
        rows = []
        for it in data:
            q = it.get("question") or it.get("target") or {}
            heat = it.get("detail_text") or ""
            if not heat:
                pv = (it.get("reaction") or {}).get("new_pv")
                heat = f"{pv // 10000}万浏览" if isinstance(pv, int) and pv >= 10000 else (f"{pv}浏览" if pv else "")
            if q.get("id"):
                rows.append({"qid": str(q["id"]), "title": q.get("title", ""), "heat": heat})
        return rows

    def answers(self, qid: str, offset: int) -> list[Doc]:
        with self.lock:
            data = self.client.get_question_answers(qid, offset=offset, limit=10).get("data", [])
        return [
            Doc(
                title=(a.get("question") or {}).get("title", ""),
                author=(a.get("author") or {}).get("name", "匿名用户"),
                votes=a.get("voteup_count", 0),
                comments=a.get("comment_count", 0),
                body=render(a.get("content", "")),
                aid=str(a["id"]),
                url=f"https://www.zhihu.com/question/{qid}/answer/{a['id']}",
                created=a.get("created_time") or 0,
                updated=a.get("updated_time") or 0,
                qid=qid,
            )
            for a in data
        ]

    def feed(self, want: int = 20, known: set[str] | frozenset[str] = frozenset()) -> list[Doc]:
        docs: list[Doc] = []
        seen = set(known)
        for _ in range(3):
            with ThreadPoolExecutor(4) as pool:
                batches = list(pool.map(lambda _: self._feed_once(), range(4)))
            for batch in batches:
                for d in batch:
                    if d.url not in seen:
                        seen.add(d.url)
                        docs.append(d)
            if len(docs) >= want:
                break
        return docs

    def _feed_once(self) -> list[Doc]:
        data = self.client.get_feed(limit=10).get("data", [])
        return [d for d in (self.doc_from_target(x.get("target") or {}) for x in data) if d]

    @staticmethod
    def doc_from_target(t: dict) -> Doc | None:
        kind = t.get("type")
        if kind not in ("answer", "article"):
            return None
        body = t.get("content") or t.get("excerpt_new") or t.get("excerpt") or t.get("preview_text") or ""
        if kind == "answer":
            q = t.get("question") or {}
            title, aid = q.get("title", ""), str(t.get("id", ""))
            url = f"https://www.zhihu.com/question/{q.get('id')}/answer/{aid}"
        else:
            title, aid = t.get("title", ""), None
            url = f"https://zhuanlan.zhihu.com/p/{t.get('id')}"
        return Doc(
            title=title or "(无标题)",
            author=(t.get("author") or {}).get("name", "匿名用户"),
            votes=t.get("voteup_count", 0),
            comments=t.get("comment_count", 0),
            body=render(body),
            aid=aid,
            url=url,
            created=t.get("created_time") or t.get("created") or 0,
            updated=t.get("updated_time") or t.get("updated") or 0,
            qid=str(q["id"]) if kind == "answer" and q.get("id") else None,
        )

    def collections(self) -> list[dict]:
        with self.lock:
            data = self.client.get_collections(limit=50).get("data", [])

        def total(cid: str) -> int:
            try:
                r = self.client._get(
                    f"https://www.zhihu.com/api/v4/collections/{cid}/items", params={"offset": 0, "limit": 1}
                )
                return (r.get("paging") or {}).get("totals") or 0
            except Exception:
                return 0

        ids = [str(c["id"]) for c in data]
        with ThreadPoolExecutor(4) as pool:
            counts = list(pool.map(total, ids))
        return [{"id": i, "title": c.get("title", ""), "count": n} for i, c, n in zip(ids, data, counts)]

    def collection_items(self, cid: str, offset: int) -> list[Doc]:
        with self.lock:
            data = self.client._get(
                f"https://www.zhihu.com/api/v4/collections/{cid}/items", params={"offset": offset, "limit": 20}
            ).get("data", [])
        return [d for d in (self.doc_from_target(x.get("content") or {}) for x in data) if d]

    def comments(self, aid: str) -> list[str]:
        with self.lock:
            data = self.client.get_answer_comments(aid, limit=20).get("data", [])
        out = []
        for c in data:
            a = c.get("author") or {}
            name = (a.get("member") or {}).get("name") or a.get("name") or "匿名用户"
            likes = c.get("like_count") or c.get("vote_count") or 0
            out.append(f"{name}  ♥{likes}\n{plain(c.get('content', ''))}")
        return out

    def vote(self, aid: str) -> bool:
        with self.lock:
            return self.client.vote_up(aid)


class Reader(Screen):
    BINDINGS = [
        ("n,right", "step(1)", "下一篇"),
        ("p,left", "step(-1)", "上一篇"),
        ("j,down", "scroll(1)", "下滚"),
        ("k,up", "scroll(-1)", "上滚"),
        ("space", "page", "翻页"),
        ("a", "question", "全部回答"),
        ("c", "comments", "评论"),
        ("v", "vote", "赞同"),
        ("o", "open", "浏览器打开"),
        ("escape,b", "app.pop_screen", "返回"),
    ]

    def __init__(self, api: Api, docs: list[Doc], idx: int, loader) -> None:
        super().__init__()
        self.api, self.docs, self.idx, self.loader = api, docs, idx, loader
        self.show_comments = False
        self.fetching = False

    def compose(self) -> ComposeResult:
        yield Static(id="meta")
        with VerticalScroll(id="body"):
            yield Static(id="text")
        yield Static(id="status")
        yield Footer()

    def on_mount(self) -> None:
        self.render_doc()

    @property
    def doc(self) -> Doc:
        return self.docs[self.idx]

    def render_doc(self) -> None:
        d = self.doc
        self.show_comments = False
        meta = Text()
        meta.append(d.title + "\n", style="bold")
        meta.append(f"{d.author}   ▲{d.votes}   评论 {d.comments}   [{self.idx + 1}/{len(self.docs)}]", style="dim")
        times = []
        if d.created:
            times.append(f"发布 {fmt_time(d.created)}")
        if d.updated and d.updated != d.created:
            times.append(f"编辑 {fmt_time(d.updated)}")
        if times:
            meta.append("\n" + "   ".join(times), style="dim")
        self.query_one("#meta", Static).update(meta)
        self.query_one("#text", Static).update(d.body if d.body.plain else Text("(无正文)"))
        self.query_one("#body", VerticalScroll).scroll_home(animate=False)

    def action_step(self, delta: int) -> None:
        new = self.idx + delta
        if new < 0:
            return
        if new < len(self.docs):
            self.idx = new
            self.render_doc()
        elif not self.fetching:
            self.fetch_more()

    @work(thread=True)
    def fetch_more(self) -> None:
        self.fetching = True
        self.app.call_from_thread(self.notify, "加载更多…")
        try:
            more = self.loader(len(self.docs))
        except Exception as e:
            self.app.call_from_thread(self.notify, f"加载失败: {e}", severity="error")
            more = []
        self.fetching = False
        if more:
            self.docs.extend(more)
            self.app.call_from_thread(self.action_step, 1)
        else:
            self.app.call_from_thread(self.notify, "没有更多了")

    def action_scroll(self, delta: int) -> None:
        self.query_one("#body", VerticalScroll).scroll_relative(y=delta * 3, animate=False)

    def action_page(self) -> None:
        self.query_one("#body", VerticalScroll).scroll_page_down(animate=False)

    def action_question(self) -> None:
        if not self.doc.qid:
            self.notify("文章没有对应的问题")
            return
        self.open_question(self.doc.qid)

    @work(thread=True)
    def open_question(self, qid: str) -> None:
        self.app.call_from_thread(self.notify, "加载回答…")
        try:
            docs = self.api.answers(qid, 0)
        except Exception as e:
            self.app.call_from_thread(self.notify, f"加载失败: {e}", severity="error")
            return
        if not docs:
            self.app.call_from_thread(self.notify, "这个问题还没有回答")
            return
        self.app.call_from_thread(
            self.app.push_screen, Reader(self.api, docs, 0, lambda n: self.api.answers(qid, n))
        )

    def action_open(self) -> None:
        webbrowser.open(self.doc.url)

    def action_comments(self) -> None:
        if not self.doc.aid:
            self.notify("文章暂不支持查看评论")
            return
        if self.show_comments:
            self.render_doc()
            return
        self.load_comments(self.doc.aid)

    @work(thread=True)
    def load_comments(self, aid: str) -> None:
        try:
            items = self.api.comments(aid)
        except Exception as e:
            self.app.call_from_thread(self.notify, f"评论加载失败: {e}", severity="error")
            return
        text = Text("\n\n".join(items) if items else "暂无评论")
        self.app.call_from_thread(self._show_comments, text)

    def _show_comments(self, text: Text) -> None:
        self.show_comments = True
        self.query_one("#text", Static).update(text)
        self.query_one("#body", VerticalScroll).scroll_home(animate=False)

    def action_vote(self) -> None:
        if self.doc.aid:
            self.do_vote(self.doc.aid)

    @work(thread=True)
    def do_vote(self, aid: str) -> None:
        try:
            ok = self.api.vote(aid)
        except Exception as e:
            self.app.call_from_thread(self.notify, f"失败: {e}", severity="error")
            return
        self.app.call_from_thread(self.notify, "已赞同" if ok else "赞同失败")


BOSS_LOG = """\
$ make -j8 test
[ 12%] Building CXX object src/core/CMakeFiles/core.dir/scheduler.cpp.o
[ 25%] Building CXX object src/core/CMakeFiles/core.dir/allocator.cpp.o
[ 41%] Building CXX object src/net/CMakeFiles/net.dir/session.cpp.o
[ 58%] Linking CXX static library libcore.a
[ 73%] Building CXX object tests/CMakeFiles/unit.dir/test_scheduler.cpp.o
[ 89%] Linking CXX executable unit
[100%] Built target unit
$ ./unit
[==========] Running 148 tests from 17 test suites.
[----------] 9 tests from SchedulerTest
[       OK ] SchedulerTest.PreemptsLowPriority (3 ms)
[       OK ] SchedulerTest.RespectsAffinity (1 ms)
[----------] 12 tests from AllocatorTest
[       OK ] AllocatorTest.CoalescesFreeBlocks (0 ms)
[       OK ] AllocatorTest.AlignsTo64 (0 ms)
[==========] 148 tests from 17 test suites ran. (412 ms total)
[  PASSED  ] 148 tests.
"""


class Boss(Screen):
    BINDINGS = [("m,escape,space", "app.pop_screen", "")]

    def compose(self) -> ComposeResult:
        yield Static(BOSS_LOG)


class Home(Screen):
    BINDINGS = [
        ("1", "mode('hot')", "热榜"),
        ("2", "mode('feed')", "推荐"),
        ("3", "mode('fav')", "收藏"),
        ("r", "reload", "刷新"),
        ("m", "boss", "老板键"),
        ("q", "app.quit", "退出"),
    ]

    def __init__(self, api: Api) -> None:
        super().__init__()
        self.api = api
        self.mode = "hot"
        self.hot: list[dict] = []
        self.feed: list[Doc] = []
        self.cols: list[dict] = []
        self.more_loading = False

    def compose(self) -> ComposeResult:
        yield Static(id="tabs")
        yield ListView(id="list")
        yield Static(id="status")
        yield Footer()

    def on_mount(self) -> None:
        self.action_mode("hot")

    def action_boss(self) -> None:
        self.app.push_screen(Boss())

    def action_reload(self) -> None:
        if self.mode == "hot":
            self.hot = []
        elif self.mode == "feed":
            self.feed = []
        else:
            self.cols = []
        self.action_mode(self.mode)

    def action_mode(self, mode: str) -> None:
        self.mode = mode
        tabs = Text()
        tabs.append(" 1 热榜 ", style="reverse bold" if mode == "hot" else "dim")
        tabs.append("  ")
        tabs.append(" 2 推荐 ", style="reverse bold" if mode == "feed" else "dim")
        tabs.append("  ")
        tabs.append(" 3 收藏 ", style="reverse bold" if mode == "fav" else "dim")
        tabs.append(f"     已登录: {self.api.me}", style="dim")
        self.query_one("#tabs", Static).update(tabs)
        if (mode == "hot" and self.hot) or (mode == "feed" and self.feed) or (mode == "fav" and self.cols):
            self.fill()
        else:
            self.query_one("#list", ListView).clear()
            self.load(mode)

    @work(thread=True, exclusive=True)
    def load(self, mode: str) -> None:
        self.app.call_from_thread(self.notify, "加载中…")
        try:
            if mode == "hot":
                self.hot = self.api.hot()
            elif mode == "feed":
                self.feed = self.api.feed()
            else:
                self.cols = self.api.collections()
        except Exception as e:
            self.app.call_from_thread(self.notify, f"加载失败: {e}", severity="error")
            return
        self.app.call_from_thread(self.fill_if, mode)

    def fill_if(self, mode: str) -> None:
        if self.mode == mode:
            self.fill()

    def fill(self) -> None:
        lv = self.query_one("#list", ListView)
        lv.clear()
        if self.mode == "hot":
            for i, r in enumerate(self.hot, 1):
                lv.append(ListItem(Label(f"{i:>2}  {r['title']}   [dim]{r['heat']}[/dim]")))
        elif self.mode == "fav":
            for c in self.cols:
                lv.append(ListItem(Label(f"{c['title']}   [dim]{c['count']} 项[/dim]")))
        else:
            for d in self.feed:
                lv.append(ListItem(Label(f"{d.title}   [dim]{d.author} ▲{d.votes}[/dim]")))
        lv.index = 0
        lv.focus()

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        lv = event.list_view
        if self.mode == "feed" and len(self.feed) > 1 and lv.index == len(self.feed) - 1 and not self.more_loading:
            self.more_loading = True
            self.more_feed()

    @work(thread=True)
    def more_feed(self) -> None:
        try:
            docs = self.api.feed(known={d.url for d in self.feed})
        except Exception as e:
            self.app.call_from_thread(self.notify, f"加载失败: {e}", severity="error")
            docs = []
        self.feed.extend(docs)
        self.app.call_from_thread(self.append_feed, docs)

    def append_feed(self, docs: list[Doc]) -> None:
        lv = self.query_one("#list", ListView)
        for d in docs:
            lv.append(ListItem(Label(f"{d.title}   [dim]{d.author} ▲{d.votes}[/dim]")))
        self.more_loading = False

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        i = event.list_view.index or 0
        items = {"hot": self.hot, "feed": self.feed, "fav": self.cols}[self.mode]
        if i >= len(items):
            return
        if self.mode == "hot":
            self.open_question(self.hot[i]["qid"])
        elif self.mode == "fav":
            self.open_collection(self.cols[i]["id"])
        else:
            self.app.push_screen(Reader(self.api, self.feed, i, lambda n: self.api.feed(want=6, known={d.url for d in self.feed})))

    @work(thread=True)
    def open_collection(self, cid: str) -> None:
        self.app.call_from_thread(self.notify, "加载收藏…")
        try:
            docs = self.api.collection_items(cid, 0)
        except Exception as e:
            self.app.call_from_thread(self.notify, f"加载失败: {e}", severity="error")
            return
        if not docs:
            self.app.call_from_thread(self.notify, "收藏夹里没有回答或文章")
            return
        self.app.call_from_thread(
            self.app.push_screen, Reader(self.api, docs, 0, lambda n: self.api.collection_items(cid, n))
        )

    @work(thread=True)
    def open_question(self, qid: str) -> None:
        self.app.call_from_thread(self.notify, "加载回答…")
        try:
            docs = self.api.answers(qid, 0)
        except Exception as e:
            self.app.call_from_thread(self.notify, f"加载失败: {e}", severity="error")
            return
        if not docs:
            self.app.call_from_thread(self.notify, "这个问题还没有回答")
            return
        self.app.call_from_thread(
            self.app.push_screen, Reader(self.api, docs, 0, lambda n: self.api.answers(qid, n))
        )


ABOUT_RULES = [
    "本软件是第三方开源工具,与知乎及其关联公司没有隶属或合作关系,「知乎」商标归其权利人所有。",
    "内容的版权归原作者和知乎所有。本软件不存储、不转载、不分发内容,只在终端里展示接口返回的数据。",
    "本软件仅供个人学习与交流,使用时请遵守知乎用户协议和相关法律法规,不得用于批量抓取或商业用途。",
    "所用接口为非官方接口,可能随时变更或失效。账号登录存在被平台限制的风险,由使用者自行承担。",
    "登录凭证只保存在本机的 ~/.zhihu-cli 目录,不会上传到任何第三方。",
    "软件按现状提供,不附带任何明示或暗示的担保。",
]


class About(ModalScreen):
    BINDINGS = [("escape,q", "app.pop_screen", "返回"), ("o", "open_repo", "打开仓库")]

    def compose(self) -> ComposeResult:
        t = Text()
        t.append(f"知乎终端 zh   v{__version__}\n\n", style="bold")
        for k, v in (
            ("开源地址", REPO_URL),
            ("许可协议", "MIT,自由软件,可免费使用、修改和分发"),
            ("数据来源", "知乎 (zhihu.com)"),
            ("依赖项目", "pyzhihu-cli (Apache-2.0)、Textual (MIT)"),
        ):
            t.append(f"{k}   ", style="dim")
            t.append(v + "\n")
        t.append("\n声明\n", style="bold")
        for i, rule in enumerate(ABOUT_RULES, 1):
            t.append(f"{i}. {rule}\n")
        with Vertical(id="dialog"):
            yield Label("关于与声明", id="dialog-title")
            with VerticalScroll(id="about-body"):
                yield Static(t)
            yield Static("o 打开仓库   Esc 返回", id="dialog-hint")

    def action_open_repo(self) -> None:
        webbrowser.open(REPO_URL)


class Settings(ModalScreen):
    BINDINGS = [("escape,s", "app.pop_screen", "关闭")]

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("设置", id="dialog-title")
            yield OptionList(id="settings-list")
            yield Static("Enter 进入   Esc 关闭", id="dialog-hint")

    def on_mount(self) -> None:
        self.refresh_items()
        self.query_one("#settings-list", OptionList).focus()

    def refresh_items(self) -> None:
        label = next((t[1] for t in THEMES if t[0] == self.app.theme), self.app.theme)
        ol = self.query_one("#settings-list", OptionList)
        idx = ol.highlighted or 0
        ol.clear_options()
        ol.add_options(
            [
                Option(f"主题   [dim]{label}[/dim]", id="theme"),
                Option(f"关于与声明   [dim]v{__version__}[/dim]", id="about"),
            ]
        )
        ol.highlighted = idx

    def on_screen_resume(self) -> None:
        self.refresh_items()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option.id == "theme":
            self.app.push_screen(ThemePicker())
        elif event.option.id == "about":
            self.app.push_screen(About())


class ThemePicker(ModalScreen):
    BINDINGS = [("escape", "cancel", "取消")]

    def __init__(self) -> None:
        super().__init__()
        self.original = ""

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("选择主题", id="dialog-title")
            yield OptionList(
                *[Option(f"{label}   [dim]{desc}[/dim]", id=tid) for tid, label, desc, _ in THEMES],
                id="themes",
            )
            yield Static("↑↓ 预览   Enter 确定   Esc 取消", id="dialog-hint")

    def on_mount(self) -> None:
        self.original = self.app.theme
        ol = self.query_one("#themes", OptionList)
        ol.highlighted = next((i for i, t in enumerate(THEMES) if t[0] == self.original), 0)
        ol.focus()

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if event.option.id:
            self.app.theme = event.option.id

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option.id:
            save_theme(event.option.id)
        self.dismiss()

    def action_cancel(self) -> None:
        self.app.theme = self.original
        self.dismiss()


class ZhihuApp(App):
    TITLE = "知乎"
    CSS = """
    Screen { background: $background; color: $foreground; }
    Screen .screen--selection { background: $primary; color: $background; }
    * { scrollbar-size-vertical: 1; }
    #status { height: 1; padding: 0 2; color: $text-muted; }
    #tabs { height: 1; margin: 0 1; }
    #meta { padding: 0 2; height: auto; background: $surface; }
    #body { padding: 1 2; }
    ListView { margin: 1 1; background: $background; }
    ThemePicker, Settings, About { align: center middle; }
    #dialog { width: 64; max-height: 90%; height: auto; border: round $primary; background: $surface; padding: 1 2; }
    #dialog-title { text-style: bold; margin-bottom: 1; }
    #dialog-hint { color: $text-muted; margin-top: 1; }
    #about-body { height: auto; max-height: 24; }
    #settings-list { height: auto; background: $surface; border: none; padding: 0; }
    #themes { height: auto; background: $surface; border: none; padding: 0; }
    """
    BINDINGS = [("q", "quit", "退出"), ("s", "settings", "设置")]
    ENABLE_COMMAND_PALETTE = False

    def __init__(self) -> None:
        super().__init__()
        self.api = Api()
        for _, _, _, theme in THEMES:
            self.register_theme(theme)
        self.theme = load_theme()

    def on_mount(self) -> None:
        self.push_screen(Home(self.api))

    def action_open_url(self, url: str) -> None:
        webbrowser.open(url)

    def action_settings(self) -> None:
        if not isinstance(self.screen, (ModalScreen, Boss)):
            self.push_screen(Settings())

    def notify(self, message, *, title="", severity="information", timeout=None, markup=True, **kw) -> None:
        try:
            status = self.screen.query_one("#status", Static)
        except Exception:
            return
        style = "bold" if severity == "error" else ""
        status.update(Text(("✗ " if severity == "error" else "") + str(message), style=style))
        if getattr(self, "_status_timer", None):
            self._status_timer.stop()
        self._status_timer = self.set_timer(3, lambda: status.update(""))


def main() -> None:
    ZhihuApp().run()


if __name__ == "__main__":
    main()
