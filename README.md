# MATCHi-bevakare 🎾

Kollar lediga tider på MATCHi med jämna mellanrum och skickar en push-notis
till mobilen när en **ny** tid som matchar dina filter dyker upp, till exempel
vid en avbokning eller när en ny dag släpps.

Skriptet läser samma schema som MATCHi:s egen sida laddar
(`/book/listSlots`). Det behöver ingen inloggning för vanliga 14 dagar.

## 1. Notiser till mobilen (2 min)

1. Installera appen **ntfy** (iOS/Android, gratis).
2. Tryck på **+** och prenumerera på topic-namnet som står under `ntfy_topic`
   i `config.toml` (t.ex. `tennis-fn04lu6coqmk`).
3. Testa: `python matchi_bevakare.py --test-notis`

Topic-namnet fungerar som ett lösenord. Den som känner till det kan läsa dina notiser,
så dela det inte.

## 2. Ställ in hallar och filter

Allt finns i `config.toml`. Filtren från början:

| Filter | Betyder |
|---|---|
| `dagar = "mån-tor"`, `start_tidigast = "18:00"` | Mån–tor, tider som börjar 18:00 eller senare |
| `dagar = "lör,sön"`, `start_tidigast = "10:00"`, `slut_senast = "17:00"` | Lör–sön, tider som ligger helt inom 10–17 |

En tid skickas om den matchar **minst ett** filter. Fler hallar lägger du till
med ytterligare `[[hall]]`-block (det räcker med `url`).

Se vad som matchar just nu, utan notis:
```
python matchi_bevakare.py --visa
```

## 3. Kör det

**A. På din egen dator** (enklast, kräver att datorn är vaken)
```
pip install -r requirements.txt
python matchi_bevakare.py --loop
```
Kollar var ~5:e minut tills du stänger fönstret (Ctrl+C).

**B. I molnet med GitHub Actions** (gratis, datorn kan vara avstängd)
1. Skapa ett **publikt** repo på GitHub och ladda upp alla filer i den här mappen.
   Publikt ger obegränsat med gratis körtid. Ett privat repo får bara ~2000
   minuter/månad, vilket tar slut med körning var 5:e minut.
2. Eftersom repot är publikt: sätt `ntfy_topic = ""` i `config.toml` och lägg
   i stället topic-namnet som hemlighet: *Settings → Secrets and variables →
   Actions → New repository secret*, namn `NTFY_TOPIC`.
3. Gå till fliken **Actions**, aktivera workflows och tryck *Run workflow* en gång för att testa.

Schemat kör var 5:e minut ca 07–23. GitHub kan ibland fördröja körningarna
5–15 minuter, så för maximal snabbhet är alternativ A bättre.

## 4. När du blir medlem (21 dagar)

Medlemsfönstret syns bara när du är inloggad, så skriptet behöver din
inloggade session-cookie (inte ditt lösenord):

1. Logga in på matchi.se i Chrome och öppna hallens sida.
2. Tryck F12 → fliken **Network** → ladda om sidan → klicka på första raden.
3. Under *Request Headers*, kopiera hela värdet för **Cookie**.
4. Sätt det som miljövariabel `MATCHI_COOKIE` (lokalt) eller som GitHub-secret
   `MATCHI_COOKIE`. Lägg det **aldrig** i `config.toml`.
5. Ändra `dagar_fram = 21` för hallen.

Cookien slutar gälla efter ett tag. Då skriver skriptet en varning i loggen
och ser bara 14 dagar igen, tills du kopierar en ny.

Lokalt på Mac/Linux:
```
export MATCHI_COOKIE='...klistra in...'
python matchi_bevakare.py --loop
```

## Bra att veta

- Skriptet **bokar inget**, det säger bara till. Notisen har en knapp som öppnar
  bokningen i MATCHi.
- Första körningen skickar en sammanfattning av vad som matchar just nu. Därefter
  kommer bara nya tider.
- Kör inte tätare än var 3–5:e minut. Det räcker gott och är schysst mot MATCHi.
- MATCHi har också en egen kölista ("Ställ mig i kö" under schemat) för en
  specifik dag och tid, med notis via mejl eller SMS.
# matchi-bevakare
# matchi-bevakare
