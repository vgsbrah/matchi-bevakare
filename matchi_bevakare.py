#!/usr/bin/env python3
"""
MATCHi-bevakare
===============
Letar efter lediga tider på MATCHi enligt dina filter och skickar en
push-notis (ntfy och/eller Telegram) när en NY tid dyker upp, t.ex. vid
en avbokning eller när nästa dag släpps.

Körsätt:
    python matchi_bevakare.py               # kör en gång (för cron / GitHub Actions)
    python matchi_bevakare.py --loop        # kör om och om igen enligt intervall_minuter
    python matchi_bevakare.py --visa        # lista alla tider som matchar just nu, ingen notis
    python matchi_bevakare.py --test-notis  # skicka en testnotis

Inställningar läses från config.toml (eller --config).
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore

import requests
from bs4 import BeautifulSoup

TZ = ZoneInfo("Europe/Stockholm")
BASE = "https://www.matchi.se"
HEADERS = {
    "User-Agent": "matchi-bevakare/1.0 (personligt skript for att hitta lediga tider)",
    "X-Requested-With": "XMLHttpRequest",
    "Accept-Language": "sv-SE,sv;q=0.9",
}

VECKODAGAR = ["mån", "tis", "ons", "tor", "fre", "lör", "sön"]
VECKODAG_KORT = ["Mån", "Tis", "Ons", "Tor", "Fre", "Lör", "Sön"]
MANADER = ["", "jan", "feb", "mar", "apr", "maj", "jun", "jul", "aug", "sep", "okt", "nov", "dec"]
# Tillåt stavning utan å/ö också
DAG_ALIAS = {"man": "mån", "lor": "lör", "son": "sön"}


def logg(msg: str) -> None:
    print(f"[{datetime.now(TZ):%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


# --------------------------------------------------------------------------- #
# Datamodell
# --------------------------------------------------------------------------- #
@dataclass
class Slot:
    id: str
    hall: str
    start: str  # ISO, Europe/Stockholm
    slut: str
    bana: str
    minuter: int
    boka_url: str

    @property
    def start_dt(self) -> datetime:
        return datetime.fromisoformat(self.start)

    @property
    def slut_dt(self) -> datetime:
        return datetime.fromisoformat(self.slut)


# --------------------------------------------------------------------------- #
# Filter
# --------------------------------------------------------------------------- #
def tolka_dagar(spec) -> set:
    """'mån-tor', 'lör,sön', 'alla', 'vardagar', 'helg' eller en lista -> {0..6}."""
    if isinstance(spec, list):
        spec = ",".join(spec)
    spec = str(spec).strip().lower()
    if spec in ("", "alla"):
        return set(range(7))
    if spec == "vardagar":
        return {0, 1, 2, 3, 4}
    if spec == "helg":
        return {5, 6}

    def idx(namn: str) -> int:
        namn = namn.strip()[:3]
        namn = DAG_ALIAS.get(namn, namn)
        if namn not in VECKODAGAR:
            raise ValueError(f"Okänd veckodag: {namn!r}")
        return VECKODAGAR.index(namn)

    dagar = set()
    for del_ in spec.split(","):
        if "-" in del_:
            a, b = (idx(x) for x in del_.split("-", 1))
            i = a
            while True:
                dagar.add(i)
                if i == b:
                    break
                i = (i + 1) % 7
        elif del_.strip():
            dagar.add(idx(del_))
    return dagar


def hhmm(s: Optional[str]) -> Optional[int]:
    if not s:
        return None
    h, m = (str(s).split(":") + ["0"])[:2]
    return int(h) * 60 + int(m)


@dataclass
class Filter:
    namn: str
    dagar: set
    start_tidigast: Optional[int]
    start_senast: Optional[int]
    slut_senast: Optional[int]
    banor: List[str]
    min_minuter: int
    hallar: List[str]

    @classmethod
    def fran_config(cls, d: dict) -> "Filter":
        return cls(
            namn=d.get("namn", "filter"),
            dagar=tolka_dagar(d.get("dagar", "alla")),
            start_tidigast=hhmm(d.get("start_tidigast")),
            start_senast=hhmm(d.get("start_senast")),
            slut_senast=hhmm(d.get("slut_senast")),
            banor=[b.lower() for b in d.get("banor", [])],
            min_minuter=int(d.get("min_minuter", 0)),
            hallar=[h.lower() for h in d.get("hallar", [])],
        )

    def matchar(self, s: Slot) -> bool:
        st, sl = s.start_dt, s.slut_dt
        if st.weekday() not in self.dagar:
            return False
        start_min = st.hour * 60 + st.minute
        slut_min = sl.hour * 60 + sl.minute
        if sl.date() > st.date():  # slutar efter midnatt
            slut_min += 24 * 60
        if self.start_tidigast is not None and start_min < self.start_tidigast:
            return False
        if self.start_senast is not None and start_min > self.start_senast:
            return False
        if self.slut_senast is not None and slut_min > self.slut_senast:
            return False
        if s.minuter < self.min_minuter:
            return False
        if self.banor and not any(b in s.bana.lower() for b in self.banor):
            return False
        if self.hallar and s.hall.lower() not in self.hallar:
            return False
        return True


# --------------------------------------------------------------------------- #
# MATCHi
# --------------------------------------------------------------------------- #
GRANS_RE = re.compile(r"max\s+(\d+)\s+(dagar|days)", re.I)


def parsa_slots(html: str, hallnamn: str, slug: str) -> List[Slot]:
    """Tolkar svaret från /book/listSlots till en lista med lediga tider."""
    soup = BeautifulSoup(html, "html.parser")
    resultat: List[Slot] = []
    for panel in soup.select("div.panel.collapse[id]"):
        m = re.fullmatch(r"(\d+)_(\d+)", panel["id"])
        if not m:
            continue
        panel_start_ms = int(m.group(2))
        for tabell in panel.select("li.list-group-item table"):
            tds = tabell.find_all("td")
            if len(tds) < 2:
                continue
            bana = " ".join(tds[0].get_text().split())
            mm = re.search(r"\d+", tds[1].get_text())
            minuter = int(mm.group()) if mm else 60

            slot_id = None
            start_ms = panel_start_ms
            slut_ms = None
            boka_url = f"{BASE}/facilities/{slug}"

            a = tabell.find("a", href=True)
            if a:
                q = parse_qs(urlparse(a["href"]).query)
                retur = q.get("returnUrl", [None])[0]
                inre = parse_qs(urlparse(retur).query) if retur else q
                slot_id = inre.get("slotIds", [None])[0]
                start_ms = int(inre.get("start", [start_ms])[0])
                if "end" in inre:
                    slut_ms = int(inre["end"][0])
                if retur:
                    boka_url = BASE + retur
                elif a["href"].startswith("/book"):
                    # Inloggad: länken går direkt till bokningen
                    boka_url = BASE + a["href"]
            if not slot_id:
                pris = tabell.find(id=re.compile(r"^price_"))
                slot_id = pris["id"][len("price_"):] if pris else f"{panel_start_ms}-{bana}"
            if slut_ms is None:
                slut_ms = start_ms + minuter * 60_000

            start = datetime.fromtimestamp(start_ms / 1000, TZ)
            slut = datetime.fromtimestamp(slut_ms / 1000, TZ)
            resultat.append(
                Slot(
                    id=slot_id,
                    hall=hallnamn,
                    start=start.isoformat(),
                    slut=slut.isoformat(),
                    bana=bana,
                    minuter=minuter,
                    boka_url=boka_url,
                )
            )
    return resultat


# Tider som syns som lediga men är reserverade för en kravprofil (t.ex. en
# stege) och släpps till alla senare. Syns bara när man öppnar bokningen.
SPARR_RE = re.compile(r"kravprofil|reserverad för kunder|reserved for customers", re.I)
SLAPP_RE = re.compile(r"(?:från|from)\s+(\d{1,2})\s+([a-zåäö]+)\s+(\d{1,2}):(\d{2})", re.I)


def tolka_slapptid(text: str, slot: Slot) -> str:
    """'... tillgänglig för alla kunder från 07 november 20:00' -> ISO-tid, '' om okänd."""
    m = SLAPP_RE.search(text)
    if not m:
        return ""
    manad = m.group(2).lower()[:3]
    if manad not in MANADER:
        return ""
    ar = slot.start_dt.year
    t = datetime(ar, MANADER.index(manad), int(m.group(1)), int(m.group(3)), int(m.group(4)), tzinfo=TZ)
    if t > slot.start_dt:  # t.ex. släpps i december för en tid i januari
        t = t.replace(year=ar - 1)
    return t.isoformat()


def kolla_sparr(sess: requests.Session, slot: Slot) -> Optional[str]:
    """Öppnar bokningen (kräver inloggning). None = går att boka,
    annars ISO-tid då spärren släpps ('' om okänt)."""
    r = sess.get(slot.boka_url, timeout=20)
    r.raise_for_status()
    if "/login" in r.url:  # utloggad – kan inte avgöra
        return None
    text = " ".join(BeautifulSoup(r.text, "html.parser").get_text().split())
    if not SPARR_RE.search(text):
        return None
    return tolka_slapptid(text, slot)


def ny_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    cookie = os.environ.get("MATCHI_COOKIE", "").strip()
    if cookie:
        # Behövs bara för att se medlemsfönstret (t.ex. 21 dagar)
        # Ta bort radbrytningar om något följt med vid kopieringen
        s.headers["Cookie"] = " ".join(cookie.split())
    return s


def hitta_facility_id(sess: requests.Session, slug: str) -> int:
    r = sess.get(f"{BASE}/facilities/{slug}", timeout=20)
    r.raise_for_status()
    m = re.search(r"facilityId=(\d+)", r.text)
    if not m:
        raise RuntimeError(f"Hittade inget facilityId för {slug}")
    return int(m.group(1))


def hamta_dag(sess: requests.Session, fid: int, dag: date, sport: int) -> str:
    r = sess.get(
        f"{BASE}/book/listSlots",
        params={"wl": "", "facility": fid, "date": dag.isoformat(), "sport": sport, "week": "", "year": ""},
        timeout=20,
    )
    r.raise_for_status()
    return r.text


def slug_fran(hall: dict) -> str:
    if "slug" in hall:
        return hall["slug"]
    url = hall.get("url", "")
    m = re.search(r"/facilities/([^/?#]+)", url)
    if not m:
        raise ValueError(f"Ange slug eller url för hallen: {hall}")
    return m.group(1)


def skanna_hall(
    sess: requests.Session, hall: dict, paus: float
) -> Tuple[List[Slot], set, bool]:
    """Returnerar (lediga tider, datum som INTE kunde hämtas, om MATCHi begränsade antalet dagar)."""
    slug = slug_fran(hall)
    namn = hall.get("namn", slug)
    fid = int(hall.get("id") or hitta_facility_id(sess, slug))
    sport = int(hall.get("sport", 1))
    dagar_fram = int(hall.get("dagar_fram", 14))

    idag = datetime.now(TZ).date()
    tider: List[Slot] = []
    misslyckade: set = set()
    begransad = False
    for i in range(dagar_fram + 1):
        dag = idag + timedelta(days=i)
        try:
            html = hamta_dag(sess, fid, dag, sport)
        except Exception as e:  # nätverksfel e.d.
            logg(f"{namn} {dag}: kunde inte hämta ({e})")
            misslyckade.add(dag.isoformat())
            continue
        dagens = parsa_slots(html, namn, slug)
        if not dagens:
            g = GRANS_RE.search(html)
            if g:
                if int(g.group(1)) < dagar_fram:
                    begransad = True
                    logg(
                        f"{namn}: MATCHi visar bara {g.group(1)} dagar fram för dig "
                        f"(dagar_fram={dagar_fram}). Är MATCHI_COOKIE satt och är du medlem?"
                    )
                break
        tider.extend(dagens)
        time.sleep(paus + random.uniform(0, paus))
    return tider, misslyckade, begransad


# --------------------------------------------------------------------------- #
# Notiser
# --------------------------------------------------------------------------- #
def formatera(tider: List[Slot], max_rader: int = 15) -> str:
    flera_hallar = len({t.hall for t in tider}) > 1
    grupper: Dict[tuple, List[Slot]] = defaultdict(list)
    for t in sorted(tider, key=lambda x: (x.start, x.hall, x.bana)):
        grupper[(t.hall, t.start, t.slut)].append(t)

    rader = []
    for (hall, start, slut), lista in grupper.items():
        st, sl = datetime.fromisoformat(start), datetime.fromisoformat(slut)
        banor = [x.bana for x in lista]
        bantext = banor[0] if len(banor) == 1 else f"{len(banor)} banor"
        rad = f"{VECKODAG_KORT[st.weekday()]} {st.day} {MANADER[st.month]} {st:%H:%M}–{sl:%H:%M} · {bantext}"
        if flera_hallar:
            rad += f" · {hall}"
        rader.append(rad)
    if len(rader) > max_rader:
        rest = len(rader) - max_rader
        rader = rader[:max_rader] + [f"…och {rest} till"]
    return "\n".join(rader)


def skicka_notis(cfg: dict, titel: str, text: str, klick_url: Optional[str] = None) -> None:
    n = cfg.get("notis", {})
    skickad = False

    topic = os.environ.get("NTFY_TOPIC") or n.get("ntfy_topic")
    server = (os.environ.get("NTFY_SERVER") or n.get("ntfy_server") or "https://ntfy.sh").rstrip("/")
    if topic:
        payload = {"topic": topic, "title": titel, "message": text, "tags": ["tennis"], "priority": 4}
        if klick_url:
            payload["click"] = klick_url
            payload["actions"] = [{"action": "view", "label": "Öppna i MATCHi", "url": klick_url}]
        try:
            r = requests.post(server, json=payload, timeout=15)
            r.raise_for_status()
            skickad = True
        except Exception as e:
            logg(f"ntfy misslyckades: {e}")

    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN") or n.get("telegram_bot_token")
    tg_chat = os.environ.get("TELEGRAM_CHAT_ID") or n.get("telegram_chat_id")
    if tg_token and tg_chat:
        msg = f"{titel}\n\n{text}" + (f"\n\n{klick_url}" if klick_url else "")
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{tg_token}/sendMessage",
                json={"chat_id": tg_chat, "text": msg, "disable_web_page_preview": True},
                timeout=15,
            )
            r.raise_for_status()
            skickad = True
        except Exception as e:
            logg(f"Telegram misslyckades: {e}")

    if not skickad:
        logg("Ingen notiskanal konfigurerad (eller alla misslyckades). Meddelandet var:")
    print(f"--- {titel} ---\n{text}\n", flush=True)


# --------------------------------------------------------------------------- #
# Huvudlogik
# --------------------------------------------------------------------------- #
def las_state(p: Path) -> Optional[dict]:
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def spara_state(
    p: Path,
    tider: Dict[str, Slot],
    varnade_hallar: List[str],
    sparrade: Optional[Dict[str, str]] = None,
    bokningsbara: Optional[set] = None,
) -> None:
    data = {
        "uppdaterad": datetime.now(TZ).isoformat(timespec="seconds"),
        "inloggning_varnad": sorted(varnade_hallar),
        "tider": {k: asdict(v) for k, v in sorted(tider.items(), key=lambda kv: kv[1].start)},
        # id -> när spärren släpps ('' = okänt, kolla igen varje körning)
        "sparrade": dict(sorted((sparrade or {}).items())),
        # redan kontrollerade som bokningsbara, behöver inte öppnas igen
        "bokningsbara": sorted(bokningsbara or ()),
    }
    p.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def kor_en_gang(cfg: dict, state_path: Path, visa: bool = False) -> None:
    filter_ = [Filter.fran_config(f) for f in cfg.get("filter", [])]
    if not filter_:
        logg("Inga filter i config — matchar alla tider.")
    paus = float(cfg.get("allmant", {}).get("paus_mellan_anrop_sek", 0.7))

    sess = ny_session()
    nu = datetime.now(TZ)
    aktuella: Dict[str, Slot] = {}
    misslyckade: Dict[str, set] = {}
    begransade: List[str] = []
    for hall in cfg.get("hall", []):
        tider, fel, begr = skanna_hall(sess, hall, paus)
        hallnamn = hall.get("namn", slug_fran(hall))
        misslyckade[hallnamn] = fel
        if begr:
            begransade.append(hallnamn)
        for t in tider:
            if t.start_dt <= nu:
                continue
            if not filter_ or any(f.matchar(t) for f in filter_):
                aktuella[t.id] = t

    state = las_state(state_path)
    forsta = state is None
    tidigare: Dict[str, Slot] = {k: Slot(**v) for k, v in (state or {}).get("tider", {}).items()}

    # Sortera bort tider som är reserverade för en kravprofil. De räknas som nya
    # (och ger notis) först när de går att boka. Kräver inloggning.
    sparrade: Dict[str, str] = {}
    sparrade_slots: Dict[str, Slot] = {}
    bokningsbara: set = set()
    if "Cookie" in sess.headers:
        kanda_ok = set((state or {}).get("bokningsbara", []))
        sparrade_fore: Dict[str, str] = (state or {}).get("sparrade", {})
        for k, t in list(aktuella.items()):
            if k in kanda_ok:
                bokningsbara.add(k)
                continue
            slapps = sparrade_fore.get(k)
            if slapps and datetime.fromisoformat(slapps) > nu:
                sparr: Optional[str] = slapps  # inte släppt än, ingen idé att kolla
            else:
                try:
                    sparr = kolla_sparr(sess, t)
                except Exception as e:
                    logg(f"{t.hall} {t.start}: kunde inte öppna bokningen ({e})")
                    sparr = slapps  # tidigare spärrad förblir spärrad, annars räknas den som ledig
                time.sleep(paus + random.uniform(0, paus))
            if sparr is None:
                bokningsbara.add(k)
            else:
                sparrade[k] = sparr
                sparrade_slots[k] = aktuella.pop(k)

    if visa:
        logg(f"{len(aktuella)} lediga tider matchar dina filter just nu:")
        print(formatera(list(aktuella.values()), max_rader=500) or "(inga)")
        if sparrade_slots:
            print(f"\n{len(sparrade_slots)} reserverade för kravprofil (notis när de släpps):")
            for k, t in sorted(sparrade_slots.items(), key=lambda kv: kv[1].start):
                s = sparrade[k]
                nar = f"{datetime.fromisoformat(s):%d/%m %H:%M}" if s else "okänt"
                print(f"{formatera([t])} · släpps {nar}")
        return

    # Datum som inte gick att hämta: behåll tidigare kända tider så att de inte
    # felaktigt "försvinner" och sedan larmar som nya nästa gång.
    for k, t in tidigare.items():
        if t.start_dt > nu and t.start_dt.date().isoformat() in misslyckade.get(t.hall, set()):
            aktuella.setdefault(k, t)

    nya = [t for k, t in aktuella.items() if k not in tidigare]
    spara_state(state_path, aktuella, begransade, sparrade, bokningsbara)

    # Säg till EN gång när medlemsfönstret slutar synas (oftast utgången cookie)
    redan_varnade = set((state or {}).get("inloggning_varnad", []))
    nyvarnade = [h for h in begransade if h not in redan_varnade]
    if nyvarnade:
        har_cookie = bool(os.environ.get("MATCHI_COOKIE", "").strip())
        orsak = (
            "Inloggningen (MATCHI_COOKIE) verkar ha slutat gälla. Kopiera en ny cookie och uppdatera secreten."
            if har_cookie
            else "MATCHI_COOKIE är inte satt, så bara tider för icke-medlemmar syns."
        )
        skicka_notis(
            cfg,
            "MATCHi-bevakaren ser färre dagar än inställt",
            f"{', '.join(nyvarnade)}: {orsak} Bevakningen fortsätter för de dagar som syns.",
        )

    if forsta:
        logg(f"Första körningen: {len(aktuella)} tider matchar just nu.")
        if aktuella:
            lista = sorted(aktuella.values(), key=lambda x: x.start)
            skicka_notis(
                cfg,
                f"Bevakning igång – {len(aktuella)} lediga tider matchar nu",
                formatera(lista),
                lista[0].boka_url,
            )
        else:
            skicka_notis(cfg, "Bevakning igång", "Inga lediga tider matchar just nu. Du får en notis när något dyker upp.")
        return

    if nya:
        nya.sort(key=lambda x: x.start)
        hallar = sorted({t.hall for t in nya})
        titel = f"{len(nya)} ny{'a' if len(nya) > 1 else ''} tid{'er' if len(nya) > 1 else ''} – {', '.join(hallar)}"
        logg(titel)
        skicka_notis(cfg, titel, formatera(nya), nya[0].boka_url)
    else:
        logg(f"Inga nya tider ({len(aktuella)} matchande lediga totalt).")


def main() -> None:
    ap = argparse.ArgumentParser(description="Bevaka lediga tider på MATCHi")
    ap.add_argument("--config", default=str(Path(__file__).with_name("config.toml")))
    ap.add_argument("--state", default=None, help="Fil där kända tider sparas (standard: state.json bredvid config)")
    ap.add_argument("--loop", action="store_true", help="Kör kontinuerligt")
    ap.add_argument("--visa", action="store_true", help="Lista matchande tider, skicka ingen notis")
    ap.add_argument("--test-notis", action="store_true", help="Skicka en testnotis och avsluta")
    args = ap.parse_args()

    cfg_path = Path(args.config)
    with open(cfg_path, "rb") as fh:
        cfg = tomllib.load(fh)
    state_path = Path(args.state) if args.state else cfg_path.with_name("state.json")

    if args.test_notis:
        skicka_notis(cfg, "Test från MATCHi-bevakaren", "Fungerar! Du kommer få notiser här.", BASE)
        return

    if not args.loop:
        kor_en_gang(cfg, state_path, visa=args.visa)
        return

    intervall = float(cfg.get("allmant", {}).get("intervall_minuter", 5))
    logg(f"Startar bevakning var ~{intervall:g}:e minut. Avbryt med Ctrl+C.")
    while True:
        try:
            kor_en_gang(cfg, state_path)
        except KeyboardInterrupt:
            raise
        except Exception as e:
            logg(f"Fel under körning: {e}")
        time.sleep(intervall * 60 * random.uniform(0.85, 1.15))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
