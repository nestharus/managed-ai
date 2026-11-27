"""Query interpretation and pure-stdlib positional Porter-stemmed BM25 analyzer."""

import hashlib
import json
import math
import re
import unicodedata
import zlib
from collections import defaultdict
from typing import Any, ClassVar

from .config import DEFAULT_LIBRARY_CONFIG, SearchConfig

SEARCH_POLICY = DEFAULT_LIBRARY_CONFIG.search.policy
TOKEN = re.compile(r"\w+")
STOPWORDS = frozenset(
    TOKEN.findall(
        "a an and are as at be been being but by can could did do does for from had has have "
        "how i if in into is it its may me my of on or our should so than that the their them "
        "then there these they this those to was we were what when where which who why will "
        "with would you your"
    )
)
PREPARED_INDEX_MAGIC = b"MAI-LEX-IDX/1\n\x00\x00"
_SQLITE_MAGIC = b"SQLite format 3\x00"
_V = frozenset("aeiou")
_S2 = tuple(
    (p[: p.index(":")], p[p.index(":") + 1 :])
    for p in re.findall(
        r"\S+",
        "ational:ate tional:tion enci:ence anci:ance izer:ize abli:able alli:al entli:ent eli:e "
        "ousli:ous ization:ize ation:ate ator:ate alism:al iveness:ive fulness:ful ousness:ous "
        "aliti:al iviti:ive biliti:ble",
    )
)
_S3 = tuple(
    (p[: p.index(":")], p[p.index(":") + 1 :])
    for p in re.findall(r"\S+", "icate:ic ative: alize:al iciti:ic ical:ic ful: ness:")
)
_S4 = tuple(
    TOKEN.findall("al ance ence er ic able ible ant ement ment ent ou ism ate iti ous ive ize")
)


def need(condition: Any, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _encoded(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _cons(w: str, i: int) -> bool:
    c = w[i]
    return False if c in _V else (i == 0 or not _cons(w, i - 1)) if c == "y" else True


def _m(w: str) -> int:
    n, i, cnt = len(w), 0, 0
    while i < n and _cons(w, i):
        i += 1
    while i < n:
        while i < n and not _cons(w, i):
            i += 1
        if i >= n:
            break
        while i < n and _cons(w, i):
            i += 1
        cnt += 1
    return cnt


def _v(w: str) -> bool:
    return any(not _cons(w, i) for i in range(len(w)))


def _dbl(w: str) -> bool:
    return len(w) >= 2 and w[-1] == w[-2] and _cons(w, len(w) - 1)


def _cvc(w: str) -> bool:
    return (
        len(w) >= 3
        and w[-1] not in "wxy"
        and _cons(w, len(w) - 1)
        and not _cons(w, len(w) - 2)
        and _cons(w, len(w) - 3)
    )


def _step1(w: str) -> str:
    if w.endswith(("sses", "ies")):
        w = w[:-2]
    elif not w.endswith("ss") and w.endswith("s"):
        w = w[:-1]
    chg = False
    if w.endswith("eed"):
        w = w[:-1] if _m(w[:-3]) > 0 else w
    elif w.endswith("ed") and _v(w[:-2]):
        w, chg = w[:-2], True
    elif w.endswith("ing") and _v(w[:-3]):
        w, chg = w[:-3], True
    if chg:
        if w.endswith(("at", "bl", "iz")):
            w += "e"
        elif _dbl(w) and w[-1] not in "lsz":
            w = w[:-1]
        elif _m(w) == 1 and _cvc(w):
            w += "e"
    return (w[:-1] + "i") if w.endswith("y") and _v(w[:-1]) else w


def _rep(w: str, tbl: tuple[tuple[str, str], ...]) -> str:
    for s, r in tbl:
        if w.endswith(s):
            return (w[: -len(s)] + r) if _m(w[: -len(s)]) > 0 else w
    return w


def _step2_to_5(w: str) -> str:
    w = _rep(_rep(w, _S2), _S3)
    for s in _S4:
        if w.endswith(s):
            w = w[: -len(s)] if _m(w[: -len(s)]) > 1 else w
            break
    else:
        if w.endswith("ion") and len(w) > 3 and w[-4] in "st" and _m(w[:-3]) > 1:
            w = w[:-3]
    if w.endswith("e") and (_m(w[:-1]) > 1 or (_m(w[:-1]) == 1 and not _cvc(w[:-1]))):
        w = w[:-1]
    return w[:-1] if (_m(w) > 1 and _dbl(w) and w.endswith("l")) else w


def porter_stem(w: str) -> str:
    return _step2_to_5(_step1(w)) if (len(w) > 2 and w.isascii() and w.isalpha()) else w


def analyze_tokens(text: str) -> list[str]:
    f = "".join(
        c for c in unicodedata.normalize("NFD", text.casefold()) if unicodedata.category(c) != "Mn"
    )
    return [porter_stem(t) for t in TOKEN.findall(f)]


def _uniq_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in pairs:
        need(k not in out, "duplicate JSON key in prepared index: " + k)
        out[k] = v
    return out


def _reject_const(val: str) -> Any:
    raise ValueError("nonfinite JSON constant in prepared index: " + val)


_DEC = json.JSONDecoder(object_pairs_hook=_uniq_pairs, parse_constant=_reject_const)


class LexicalTable:
    """Contentless positional inverted index over 4 weighted fields."""

    def __init__(self) -> None:
        self.keys: list[Any] = []
        self.lengths: list[tuple[int, int, int, int]] = []
        self.postings: dict[str, dict[int, dict[int, list[int]]]] = {}

    def add(self, key: Any, id_val: str, title: str, summary: str, body: str) -> None:
        idx = len(self.keys)
        self.keys.append(key)
        lens = []
        for fi, raw in enumerate((id_val, title, summary, body)):
            toks = analyze_tokens(raw)
            lens.append(len(toks))
            for pos, t in enumerate(toks):
                self.postings.setdefault(t, {}).setdefault(idx, {}).setdefault(fi, []).append(pos)
        self.lengths.append((lens[0], lens[1], lens[2], lens[3]))

    def _uf(self, unit: str, w: tuple[int, int, int, int]) -> dict[int, float]:
        ts = analyze_tokens(unit)
        if not ts:
            return {}
        if len(ts) == 1:
            dm = self.postings.get(ts[0])
            return (
                {d: float(sum(w[f] * len(p) for f, p in fm.items())) for d, fm in dm.items()}
                if dm
                else {}
            )
        rm = [m for t in ts if (m := self.postings.get(t)) is not None]
        if len(rm) != len(ts):
            return {}
        out: dict[int, float] = {}
        for d in set(rm[0]).intersection(*(m.keys() for m in rm[1:])):
            h = 0.0
            for fi in range(4):
                res = [lst for m in rm if (lst := m[d].get(fi))]
                if len(res) == len(rm):
                    ss = [set(p) for p in res[1:]]
                    h += w[fi] * sum(
                        1 for p0 in res[0] if all((p0 + k) in s for k, s in enumerate(ss, 1))
                    )
            if h > 0.0:
                out[d] = h
        return out

    def search(self, units: list[str], w: tuple[int, int, int, int]) -> dict[Any, dict[str, Any]]:
        n = len(self.keys)
        if n == 0 or not units:
            return {}
        wl = [float(sum(w[f] * r[f] for f in range(4))) for r in self.lengths]
        avg = (sum(wl) / n) or 1.0
        sc: dict[int, float] = defaultdict(float)
        mt: dict[int, list[str]] = defaultdict(list)
        for u in units:
            uf = self._uf(u, w)
            if not uf:
                continue
            idf = math.log1p((n - len(uf) + 0.5) / (len(uf) + 0.5))
            for d, fr in uf.items():
                sc[d] += idf * fr * 2.2 / (fr + 1.2 * (0.25 + 0.75 * wl[d] / avg))
                mt[d].append(u)
        return {self.keys[d]: {"score": s, "matched_terms": mt[d]} for d, s in sc.items()}

    def to_payload(self) -> dict[str, Any]:
        return {
            "keys": list(self.keys),
            "lengths": [list(r) for r in self.lengths],
            "postings": {
                t: [[d, f, p] for d, fm in sorted(dm.items()) for f, p in sorted(fm.items())]
                for t, dm in sorted(self.postings.items())
            },
        }

    @classmethod
    def from_payload(cls, p: Any) -> LexicalTable:
        if not isinstance(p, dict):
            raise ValueError("invalid prepared lexical table")
        ks, ls, ps = p.get("keys"), p.get("lengths"), p.get("postings")
        if (
            not isinstance(ks, list)
            or not isinstance(ls, list)
            or len(ks) != len(ls)
            or not isinstance(ps, dict)
        ):
            raise ValueError("invalid prepared lexical table structure")
        tbl = cls()
        for k in ks:
            need(isinstance(k, (int, str)) and type(k) is not bool, "invalid prepared lexical key")
        tbl.keys = list(ks)
        for r in ls:
            need(
                isinstance(r, list) and len(r) == 4 and all(type(v) is int and v >= 0 for v in r),
                "invalid prepared lexical lengths",
            )
            tbl.lengths.append((r[0], r[1], r[2], r[3]))
        for t, es in ps.items():
            need(isinstance(t, str) and isinstance(es, list), "invalid prepared lexical postings")
            dm: dict[int, dict[int, list[int]]] = {}
            for e in es:
                need(
                    isinstance(e, list)
                    and len(e) == 3
                    and type(e[0]) is int
                    and 0 <= e[0] < len(ks)
                    and type(e[1]) is int
                    and 0 <= e[1] < 4
                    and isinstance(e[2], list)
                    and all(type(x) is int and x >= 0 for x in e[2]),
                    "invalid prepared lexical posting entry",
                )
                dm.setdefault(e[0], {})[e[1]] = list(e[2])
            tbl.postings[t] = dm
        return tbl

    def estimated_bytes(self) -> int:
        return (
            len(self.keys) * 48
            + len(self.lengths) * 32
            + sum(
                len(t.encode())
                + 48
                + sum(24 + len(p) * 8 for fm in dm.values() for p in fm.values())
                for t, dm in self.postings.items()
            )
        )


class LexicalStore:
    """In-memory and serializable pure-stdlib container of contentless LexicalTables."""

    def __init__(self) -> None:
        self.tables: dict[str, LexicalTable] = {}
        self.basis: dict[str, Any] | None = None
        self.closed = False

    def create_table(self, name: str) -> LexicalTable:
        need(not self.closed, "lexical index store is closed")
        t = LexicalTable()
        self.tables[name] = t
        return t

    def table(self, name: str) -> LexicalTable:
        need(not self.closed, "lexical index store is closed")
        if (found := self.tables.get(name)) is None:
            raise ValueError("unknown lexical index table: " + name)
        return found

    def serialize(self, basis: dict[str, Any]) -> bytes:
        need(not self.closed, "lexical index store is closed")
        raw = json.dumps(
            {"basis": basis, "tables": {k: t.to_payload() for k, t in sorted(self.tables.items())}},
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return PREPARED_INDEX_MAGIC + zlib.compress(raw, level=6)

    @classmethod
    def deserialize(
        cls, data: bytes, *, max_uncompressed_bytes: int = 128 * 1024 * 1024
    ) -> LexicalStore:
        need(
            not data.startswith(_SQLITE_MAGIC),
            "legacy SQLite prepared index is unsupported; rebuild with prepare-index",
        )
        need(data.startswith(PREPARED_INDEX_MAGIC), "invalid prepared index header")
        dec = zlib.decompressobj()
        try:
            raw = dec.decompress(data[len(PREPARED_INDEX_MAGIC) :], max_uncompressed_bytes + 1)
            need(
                len(raw) <= max_uncompressed_bytes and not dec.unconsumed_tail,
                "prepared index exceeds decompressed byte bound",
            )
            raw += dec.flush((max_uncompressed_bytes + 1) - len(raw))
            need(
                len(raw) <= max_uncompressed_bytes and dec.eof and not dec.unused_data,
                "corrupt or truncated prepared index",
            )
        except zlib.error as exc:
            raise ValueError(f"corrupt prepared index: {exc}") from None
        p = _DEC.decode(raw.decode("utf-8"))
        need(
            isinstance(p, dict)
            and isinstance(p.get("basis"), dict)
            and isinstance(p.get("tables"), dict),
            "invalid prepared index structure",
        )
        st = cls()
        st.basis = p["basis"]
        for name, tp in p["tables"].items():
            need(isinstance(name, str), "invalid prepared index table name")
            st.tables[name] = LexicalTable.from_payload(tp)
        return st

    def estimated_bytes(self) -> int:
        return 0 if self.closed else sum(t.estimated_bytes() for t in self.tables.values())

    def close(self) -> None:
        self.tables.clear()
        self.basis = None
        self.closed = True


def search_query(
    query: str, backend: str, *, config: SearchConfig = DEFAULT_LIBRARY_CONFIG.search
) -> dict[str, Any]:
    need(backend in {"bm25", "fts5", "literal", "membus"}, "unknown discovery backend")
    if backend == "literal":
        return {
            "terms": list(dict.fromkeys(query.casefold().split())),
            "phrases": [],
            "ignored_terms": [],
            "tokenization": "casefolded whitespace substrings",
            "language": "no stemming or stopword policy",
        }
    need(query.count('"') % 2 == 0, "unclosed double quote in query")
    terms: list[str] = []
    phrases: list[str] = []
    ignored: list[str] = []
    for m in re.finditer(r'"([^"]*)"|([^\s"]+)', query):
        if m.group(1) is not None:
            if ph := " ".join(TOKEN.findall(m.group(1).casefold())):
                phrases.append(ph)
        else:
            for t in TOKEN.findall(m.group(2).casefold()):
                (ignored if t in STOPWORDS else terms).append(t)
    terms, phrases, ignored = (
        list(dict.fromkeys(terms)),
        list(dict.fromkeys(phrases)),
        list(dict.fromkeys(ignored)),
    )
    need(
        len(terms) + len(phrases) <= config.max_distinct_units,
        f"query exceeds {config.max_distinct_units} distinct terms and phrases",
    )
    return {
        "terms": terms,
        "phrases": phrases,
        "ignored_terms": ignored,
        "tokenization": "whole Unicode word tokens; quoted token phrases",
        "language": "English stopwords; English porter stemming"
        if backend == "fts5"
        else "English stopwords; no stemming",
    }


class FtsAnalyzer:
    weights = DEFAULT_LIBRARY_CONFIG.search.fts_weights
    tokenizer = DEFAULT_LIBRARY_CONFIG.search.fts_tokenizer
    keys: ClassVar[dict[str, str]] = {"docs": "id", "body_docs": "rowid", "module_docs": "rowid"}

    @classmethod
    def new_store(cls) -> LexicalStore:
        return LexicalStore()

    @classmethod
    def identity(cls, config: SearchConfig = DEFAULT_LIBRARY_CONFIG.search) -> dict[str, Any]:
        return {
            "engine": "pure-stdlib-inverted-bm25/1",
            "tokenizer": config.fts_tokenizer,
            "weights": list(config.fts_weights),
            "detail": "full",
            "columnsize": 1,
            "unicode_version": unicodedata.unidata_version,
            "word_pattern": TOKEN.pattern,
            "stopwords_sha256": _digest(_encoded(sorted(STOPWORDS))),
        }

    @classmethod
    def create(
        cls, db: Any, table: str, config: SearchConfig = DEFAULT_LIBRARY_CONFIG.search
    ) -> None:
        del config
        need(table in cls.keys, "unknown lexical table: " + table)
        db.create_table(table)

    @classmethod
    def add(
        cls, db: Any, table: str, key: Any, id_val: str, title: str, summary: str, body: str
    ) -> None:
        db.table(table).add(key, id_val, title, summary, body)

    @classmethod
    def search(
        cls,
        db: Any,
        table: str,
        units: list[str],
        config: SearchConfig = DEFAULT_LIBRARY_CONFIG.search,
    ) -> dict[Any, dict[str, Any]]:
        need(table in cls.keys, "unknown lexical table: " + table)
        return dict(db.table(table).search(units, config.fts_weights))
