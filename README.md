# SendSms 📱

En stilren, responsiv webbapplikation skräddarsydd för butiker och verkstäder – för att enkelt skicka SMS till kunder (t.ex. när varor är redo för upphämtning, vid förseningar eller för påminnelser).

Utvecklad av: **Dennis West** (+46 736562525)

---

## Funktioner 🚀

- **Skicka SMS:** Fyll i nummer, vara och belopp. Välj en mall och skicka iväg! Appen formaterar summor (t.ex. 1 500 kr) och teckenräknar i realtid.
- **Smidig Historik:** Sökbar historik i realtid där du enkelt kan bocka av varor som "uthämtade".
- **Påminnelser:** Skicka påminnelser med ett klick direkt från historiken. Appen håller automatiskt räkning på hur många påminnelser en kund fått och visar datum för dessa.
- **Påminnelse-räknare:** En klickbar blå siffra bredvid påminnelseknappen visar antal skickade påminnelser. Klicka på den för att se exakta datum.
- **Felsökning:** Klickbara "Failed"-statusar som visar det exakta felmeddelandet från 46elks API:et i en modal.
- **Saldo:** Ditt aktuella saldo hos 46elks visas direkt i toppmenyn och uppdateras automatiskt efter varje skickat SMS.
- **Redigera Mallar:** Skapa och ändra obegränsat med egna SMS-mallar inifrån appen.
- **Säkerhet:** Lås appen med en fyrsiffrig PIN-kod (stöd för Master-PIN via miljövariabel).
- **Logotyp:** Stöd för egna logotyper (`logo-light.png` och `logo-dark.png`) som automatiskt växlar beroende på tema. Loggan används även som favicon/bokmärkesikon.
- **Dark Mode:** Elegant inbyggt stöd för mörkt och ljust läge – logotypen byter automatiskt.
- **Övningsläge:** Träna och testa gränssnittet tryggt utan att skicka riktiga SMS. En röd ram visas runt hela appen som tydlig varning.
- **GDPR-vänlig:** Rensar gamla loggar automatiskt. Antal dagar konfigureras direkt i appens inställningar (standard: 90 dagar).
- **Responsiv design:** Fungerar på dator, surfplatta och mobil.
- **Om-sida:** Inbyggd informationsruta med kontaktuppgifter och ansvarsfriskrivning.

---

## Filstruktur 📁

```
SendSms/
├── app.py                  # Backend (Flask/Python)
├── requirements.txt        # Python-beroenden
├── Dockerfile              # Docker-konfiguration
├── README.md               # Denna fil
├── static/
│   ├── logo-light.png      # Logotyp för ljust läge & favicon
│   └── logo-dark.png       # Logotyp för mörkt läge
├── templates/
│   └── index.html          # All frontend (HTML/CSS/JS)
└── data/                   # Skapas automatiskt (databas)
    └── sms_logg.db
```

---

## Köra lokalt (Windows) 💻

Perfekt för att testa och utveckla utan att behöva Unraid/Docker.

### Förutsättningar
- [Python 3.11+](https://www.python.org/downloads/) installerat

### Steg för steg

1. Öppna en terminal (PowerShell eller Kommandotolken) och gå till mappen:
   ```powershell
   cd "C:\Users\DittNamn\Desktop\Egna appar\SendSms"
   ```

2. Skapa och aktivera en virtuell Python-miljö:
   ```powershell
   python -m venv venv
   .\venv\Scripts\activate
   ```
   *(Du ser `(venv)` i terminalen när det är aktiverat)*

3. Installera beroenden:
   ```powershell
   pip install -r requirements.txt
   ```

4. Starta appen:
   ```powershell
   python app.py
   ```

5. Öppna webbläsaren och gå till:
   **http://localhost:5000**

Tryck `Ctrl + C` i terminalen för att stänga ner appen.

---

## Installation på Windows (enklaste sättet) 🪟

I mappen finns två färdiga `.bat`-filer som gör allt automatiskt:

| Fil | Syfte |
|---|---|
| `Installera SendSms.bat` | Kör **en gång** för att installera Python-beroenden och öppna brandväggen |
| `Starta SendSms.bat` | Kör **varje dag** för att starta servern |

### Steg för steg

1. **Högerklicka** på `Installera SendSms.bat` → välj **"Kör som administratör"**
   - Installerar alla beroenden automatiskt
   - Skapar en brandväggsregel så att mobiltelefoner på samma WiFi kan nå appen
   - Kräver att [Python 3.11+](https://www.python.org/downloads/) är installerat *(kryssa i "Add Python to PATH" vid installationen!)*

2. **Dubbelklicka** på `Starta SendSms.bat` för att starta servern
   - Fönstret visar tydligt vilken adress du ska använda på datorn och mobilen:
   ```
   Öppna appen på DENNA dator:   http://localhost:5000
   Öppna appen på MOBIL (samma WiFi):  http://192.168.1.105:5000
   ```
   - Webbläsaren öppnas automatiskt på datorn

3. På mobilen: anslut till **samma WiFi** som datorn och skriv in adressen som visas i fönstret.

4. **Stäng fönstret** för att stänga av servern.

> [!NOTE]
> Mobilen och datorn måste vara på **samma WiFi-nätverk**. Appen är aldrig tillgänglig via internet – det är bara lokalt i butiken.

---

## Installation (Docker / Unraid) 🐳


### Steg 1: Kopiera filer till Unraid
Kopiera hela mappen (inklusive `static/`-mappen med logotyperna) till din Unraid-share, t.ex. `\\UNRAID\data\apps\my_apps\sendsms`.

### Steg 2: Bygg Docker-imagen i Unraid-terminalen
Öppna terminalen i Unraid-gränssnittet och kör:
```bash
cd /mnt/user/data/apps/my_apps/sendsms
docker build -t sendsms-app .
```
Lägg till `--no-cache` om du vill tvinga en fullständig ombyggnad efter en uppdatering:
```bash
docker build --no-cache -t sendsms-app .
```

### Steg 3: Lägg till i Unraid Docker-GUI
1. Gå till fliken **Docker** → **Add Container**.
2. Fyll i:
   - **Name:** `SendSms`
   - **Repository:** `sendsms-app`
   - **Network Type:** `Bridge`
3. Lägg till **Port**: Container Port `5000` → Host Port valfri (t.ex. `5858`)
4. Lägg till **Path**: Container Path `/app/data` → Host Path t.ex. `/mnt/user/data/apps/my_apps/sendsms/data`
   *(Kritiskt! Historik och inställningar sparas här.)*
5. Lägg till **Variable**: Key `TZ` → Value `Europe/Stockholm`
6. *(Frivilligt)* Lägg till **Variable**: Key `MASTER_PIN` → Value `din-nödkod`
7. Klicka på **Apply**.

### Steg 4: Uppdatera appen
När du uppdaterar koden:
1. Kopiera de ändrade filerna till Unraid-mappen.
2. Kör `docker build --no-cache -t sendsms-app .` i Unraid-terminalen.
3. Gå till Docker-fliken i Unraid → klicka på containern → **Edit** → **Apply** (tvingar Unraid att skapa en ny container med den nya koden).

---

## Konfiguration av 46elks (API) ⚙️

För att kunna skicka riktiga SMS krävs ett konto hos [46elks.se](https://46elks.se/).

1. Öppna appen i webbläsaren.
2. Klicka på **Inställningar** uppe till höger → **App & API**.
3. Klistra in ditt *API Username* och *API Password* från 46elks-sidan.
4. Skriv in ett **Avsändarnamn** (t.ex. *Butiken*).
   > ⚠️ SMS-standarden kräver att text-avsändare är mellan 3–11 tecken (inga å, ä, ö eller specialtecken).
5. Ställ in antal **dagar att spara historik** (GDPR-inställning, standard 90 dagar).
6. Spara. Ditt saldo visas nu direkt i toppmenyn!

---

## Driftsäkerhet & Backup 🔒

| Scenario | Lösning |
|---|---|
| Strömavbrott | Anslut Unraid till en **UPS** (batteribackup) |
| Unraid nere | Starta appen lokalt med `Starta SendSms.bat` |
| Glömt PIN (Unraid) | Se guide nedan |
| Glömt PIN (Windows) | Kör `Återställ PIN.bat` – se guide nedan |
| Databaskorruption | Kopiera `data/sms_logg.db` regelbundet som backup |

### Glömt PIN-koden på Windows?

I mappen finns filen **`Återställ PIN.bat`**:

1. Stäng appen/servern (stäng `Starta SendSms.bat`-fönstret)
2. Dubbelklicka på `Återställ PIN.bat`
3. Bekräfta med att skriva `JA`
4. Starta appen igen – du får välja en ny PIN direkt

> ⚠️ Nollställer bara PIN-koden. Historik, inställningar och mallar berörs **inte**.

### Glömt PIN-koden på Unraid?

**Alternativ 1 – MASTER_PIN** *(kräver att du satt upp detta i förväg!)*

MASTER_PIN är en *valfri* reservkod som du måste ha ställt in *innan* du glömmer PIN:en. Den sätts som en miljövariabel i Unraid Docker-GUI:
- Key: `MASTER_PIN` / Value: t.ex. `9999`

Om du gjort detta kan du logga in med den koden när som helst.

**Alternativ 2 – Nollställ via Unraid-terminalen** *(fungerar alltid)*

Öppna terminalen i Unraid och kör:
```bash
docker exec SendSms python -c "import sqlite3; conn=sqlite3.connect('/app/data/sms_logg.db'); conn.execute(\"UPDATE settings SET value='' WHERE key='app_pin'\"); conn.commit(); print('PIN nollställd!')"
```
Ladda om appen i webbläsaren – du får välja en ny PIN direkt.

---

## Ansvarsfriskrivning

Programvaran tillhandahålls i befintligt skick ("as is"), utan några garantier av något slag. Fel eller driftstopp kan förekomma. Utvecklaren tar inget ansvar för eventuella direkta eller indirekta skador, kostnader, förlorad data eller misslyckade SMS-utskick som kan uppstå vid användning av appen. All användning sker på egen risk.
