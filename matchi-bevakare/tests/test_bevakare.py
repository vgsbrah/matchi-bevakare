import sys, json, tempfile
from pathlib import Path
from datetime import datetime, timedelta
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import matchi_bevakare as mb

fx = (Path(__file__).parent / "fixture_2026-11-08.html").read_text()

# 1) Parser mot riktigt utdrag
s = mb.parsa_slots(fx, "Stockholms Tennishall", "stockholmstennishall")
for x in s: print(x.start, x.slut, x.bana, x.minuter, x.id[:8], x.boka_url[:70])
assert len(s) == 3
assert s[0].start_dt.hour == 8 and s[0].start_dt.day == 8 and s[0].slut_dt.hour == 9
assert s[2].start_dt.hour == 22 and s[2].bana == "Prel bana 1"
assert s[0].boka_url.startswith("https://www.matchi.se/book/index?slotIds=a1835ee1")
assert mb.parsa_slots("<div>Stockholms Tennishall &amp; Stockholms TK tillåter bokningar max 14 dagar framåt i tiden.</div>", "x", "y") == []

# 2) Dagtolkning
assert mb.tolka_dagar("mån-tor") == {0,1,2,3}
assert mb.tolka_dagar("lör,sön") == {5,6}
assert mb.tolka_dagar("lor-son") == {5,6}
assert mb.tolka_dagar("fre-mån") == {4,5,6,0}
assert mb.tolka_dagar("helg") == {5,6}

# 3) Filter enligt config
import tomllib
cfg = tomllib.loads((Path(__file__).resolve().parent.parent / "config.toml").read_text())
F = [mb.Filter.fran_config(f) for f in cfg["filter"]]
def slot(y,mo,d,h,mins=60):
    st = datetime(y,mo,d,h,tzinfo=mb.TZ); return mb.Slot("x","H",st.isoformat(),(st+timedelta(minutes=mins)).isoformat(),"Prel bana 1",mins,"u")
def ok(sl): return any(f.matchar(sl) for f in F)
assert ok(slot(2026,11,2,18))      # mån 18
assert ok(slot(2026,11,5,22))      # tor 22
assert not ok(slot(2026,11,2,17))  # mån 17
assert not ok(slot(2026,11,6,19))  # fre 19
assert ok(slot(2026,11,7,10))      # lör 10-11
assert ok(slot(2026,11,8,16))      # sön 16-17
assert not ok(slot(2026,11,8,17))  # sön 17-18 (slutar efter 17)
assert not ok(slot(2026,11,8,9))   # sön 09
assert not ok(slot(2026,11,8,22))  # sön 22
print("Filter OK")

# 4) Simulerad körning: första körning, sedan en avbokning dyker upp
tmpl = fx
def gen(dag, timmar):
    out = ['<div id="collapse-items">']
    for h in timmar:
        st = int(datetime(dag.year,dag.month,dag.day,h,tzinfo=mb.TZ).timestamp()*1000)
        out.append(f'<div class="panel panel-default collapse" id="1931_{st}"><ul><li class="list-group-item"><table><tr><td> Prel bana 3 </td><td>60min</td><td></td><td><a href="/login/auth?returnUrl=%2Fbook%2Findex%3FslotIds%3Did-{dag}-{h}%26facilityId%3D1931%26start%3D{st}%26end%3D{st+3600000}%26sportIds%3D1">Boka</a></td></tr></table></li></ul></div>')
    out.append("</div>"); return "".join(out)

fria = {}  # dag -> timmar
idag = datetime.now(mb.TZ).date()
for i in range(15):
    d = idag + timedelta(days=i)
    fria[d] = [9, 13, 20] if i > 0 else []
def fake_hamta(sess, fid, dag, sport):
    if dag > idag + timedelta(days=14):
        return "<div>tillåter bokningar max 14 dagar framåt</div>"
    return gen(dag, fria.get(dag, []))
mb.hamta_dag = fake_hamta
mb.time.sleep = lambda *_: None
skickat = []
mb.skicka_notis = lambda cfg, t, txt, url=None: skickat.append((t, txt))

with tempfile.TemporaryDirectory() as tmp:
    st = Path(tmp)/"state.json"
    cfg["hall"][0]["dagar_fram"] = 21   # testa gränsmeddelandet
    mb.kor_en_gang(cfg, st)
    print("Körning 1:", [t for t,_ in skickat]); n1 = len(skickat)
    assert n1 == 2 and "färre dagar" in skickat[0][0], "varning om cookie ska skickas en gång"
    mb.kor_en_gang(cfg, st)
    assert len(skickat) == n1, "ska inte larma när inget ändrats"
    # avbokning: en mån/tis/ons/tor 18:00 + en fredag 18 (ska ignoreras)
    vard = next(d for d in fria if d.weekday() in (0,1,2,3) and d > idag)
    fre = next(d for d in fria if d.weekday() == 4 and d > idag)
    fria[vard] = fria[vard] + [18]; fria[fre] = fria[fre] + [18]
    mb.kor_en_gang(cfg, st)
    print("Körning 3:", skickat[-1][0]); print(skickat[-1][1])
    assert len(skickat) == n1 + 1 and "18:00" in skickat[-1][1] and "1 ny tid" in skickat[-1][0]
    # tiden bokas bort och dyker upp igen -> ny notis
    fria[vard].remove(18); mb.kor_en_gang(cfg, st); fria[vard].append(18); mb.kor_en_gang(cfg, st)
    assert len(skickat) == n1 + 2
    # nätverksfel en dag ska inte ge falsklarm
    orig = mb.hamta_dag
    def flaky(sess, fid, dag, sport):
        if dag == vard: raise IOError("timeout")
        return orig(sess, fid, dag, sport)
    mb.hamta_dag = flaky; mb.kor_en_gang(cfg, st); mb.hamta_dag = orig; mb.kor_en_gang(cfg, st)
    assert len(skickat) == n1 + 2, "falsklarm efter nätverksfel"
print("Simulering OK")
inl = '<div class="panel panel-default collapse" id="1931_1794121200000"><ul><li class="list-group-item"><table><tr><td> Bana 1 </td><td>60min</td><td></td><td><a href="/book/index?slotIds=abc%26x&facilityId=1931&start=1794121200000&end=1794124800000">Boka</a></td></tr></table></li></ul></div>'
x = mb.parsa_slots(inl, "H", "h")[0]; assert x.boka_url.startswith("https://www.matchi.se/book/index") and x.slut_dt.hour == 9
print("Inloggat format OK")
print(mb.formatera([*s]))
