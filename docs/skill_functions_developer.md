# Skill-Funktionen schreiben

Ein Skill kann eine Code-Datei mitbringen. Deren Funktionen ruft das LLM in
seiner Antwort auf, die Plattform führt sie aus und ersetzt den Aufruf durch
das Ergebnis. Damit kommen Live-Daten in eine Antwort, die sonst nur aus
Dokumenten bestehen könnte.

Wer wissen will, *wie* das intern abläuft: `docs/skill_functions_architecture.md`.

---

## 1. In fünf Minuten zum ersten Funktions-Skill

**Verwaltung → Skills → Skill anlegen**, dann im Skill „Bearbeiten" →
**Ausführbare Funktionen** → Code-Datei hinterlegen.

Der Dateiname entscheidet über die Sprache: `.py` → Python, `.js` →
JavaScript. **Pro Skill genau eine Datei** — nicht gemischt.

```python
# beispiel.py

# Verdoppelt eine Zahl.
def verdoppeln(zahl):
    return zahl * 2
```

Speichern. Die Plattform liest die Datei einmal ein und zeigt an, was sie
gefunden hat:

```
beispiel.verdoppeln(zahl)   kein Netz
```

Damit ist die Funktion aufrufbar. Im `detailed_task` des Skills steht, *wann*
das LLM sie benutzen soll — dieser Text landet im System-Prompt:

```
Wenn nach dem Doppelten einer Zahl gefragt wird, schreibe
beispiel.verdoppeln(<zahl>) wörtlich an die Stelle, an der das
Ergebnis stehen soll. Rechne niemals selbst.
```

Antwortet das Modell mit „Das Doppelte ist `beispiel.verdoppeln(21)`", liest
der Nutzer „Das Doppelte ist 42".

---

## 2. Wie das Modell eine Funktion aufruft

Normalerweise als **Action**, genau wie eine Suche:

```
ACTION: SKILL({"skill": "wetter", "function": "aktuell", "args": ["Nürnberg"]})
OBSERVATION: 20 °C, überwiegend klar
```

Das Ergebnis kommt als OBSERVATION zurück, *bevor* die Antwort steht — das
Modell kann es deuten und mit Dokumenttreffern kombinieren. Im `detailed_task`
beschreibst du deshalb, **wann** die Funktion gebraucht wird, nicht wie sie
geschrieben wird.

Als Rückfall funktioniert weiterhin auch der Aufruf im Antworttext
(`wetter.aktuell("Nürnberg")`); er wird nach der Antwort durch das Ergebnis
ersetzt. Dafür gilt das folgende Format.

## 3. Aufrufformat (Textmuster-Rückfall)

```
skillname.funktionsname(argument1, argument2)
```

`skillname` ist der Name des Skills, nicht der Dateiname. Groß-/Kleinschreibung
ist beim Auflösen egal.

Argumente werden erkannt als:

| Geschrieben | Kommt an als |
|---|---|
| `"Nürnberg"` / `'Nürnberg'` | String |
| `Nürnberg` (ohne Anführungszeichen) | String |
| `42`, `-3.5` | Zahl |
| `true`, `false` | Boolean |
| `null`, `none` | `None` / `null` |
| `[1, 2]`, `{"a": 1}` | Liste / Objekt |

Klammern innerhalb von Anführungszeichen sind erlaubt:
`wetter.aktuell("Frankfurt (Oder)")` funktioniert.

Grenzen pro Antwort: **8 Aufrufe**, **8 Argumente**, **512 Zeichen** je
String-Argument. Wird eine Grenze überschritten, bleibt der Aufruf als Text
stehen, statt ausgeführt zu werden.

---

## 4. Welche Funktionen aufrufbar sind

Aufrufbar ist jede Funktion, die

* auf oberster Ebene der Datei steht (nicht eingerückt, nicht verschachtelt) und
* deren Name **nicht** mit `_` beginnt.

`_geocode(...)` ist damit ein privater Helfer: benutzbar innerhalb der Datei,
nicht von außen aufrufbar.

**Python**

```python
def aktuell(ort):        # aufrufbar
    return _helfer(ort)

def _helfer(ort):        # privat
    return ort
```

**JavaScript** — `function`, `async function` und Pfeilfunktionen, jeweils
optional mit `export`:

```javascript
export async function holen(id) { ... }   // aufrufbar
const _intern = (x) => x;                 // privat
```

---

## 5. Markierungen

Markierungen stehen in Kommentarzeilen, die **direkt** an der
Funktionsdefinition kleben — darüber oder als erste Zeilen im Rumpf.

> **Eine Leerzeile beendet den Block.** Das ist Absicht: würden Leerzeilen
> übersprungen, könnte der Kommentar einer Funktion versehentlich für die
> nächste gelten — und `ask for permission` bei der falschen Funktion zu
> landen fällt niemandem auf.

### `ask for permission`

```python
# ask for permission
# Legt einen Wartungsauftrag an.
def auftrag_anlegen(anlage, beschreibung):
    ...
```

```javascript
// ask for permission
// Legt einen Wartungsauftrag an.
export async function auftragAnlegen(anlage, beschreibung) { ... }
```

Vor der Ausführung erscheint im Chat eine Rückfrage mit Funktionsname,
Argumenten und Beschreibung. Ohne ausdrückliches „Erlauben" läuft nichts —
auch eine unbeantwortete Rückfrage gilt nach Ablauf der Frist als Ablehnung.

**Faustregel:** Markiere alles, was schreibt, Geld kostet, etwas verschickt
oder personenbezogene Daten berührt. Reine Lesezugriffe auf öffentliche Daten
brauchen die Markierung nicht.

### Das Häkchen im Dashboard

Die Markierung im Quelltext ist die **Voreinstellung**, nicht das letzte Wort.
Im Skill-Panel trägt jede Funktion ein Häkchen **„Vor dem Ausführen im Chat
nachfragen"**. Es wirkt sofort und überschreibt die Markierung in beide
Richtungen; weicht es vom Quelltext ab, steht daneben *weicht vom Code ab*.

Gespeichert wird es **getrennt** vom abgeleiteten Flag. Wichtig ist das, weil
„Code speichern" die Funktionstabelle aus dem Quelltext neu schreibt: mit nur
einem Feld wäre eine im Dashboard eingeschaltete Rückfrage beim nächsten
Tippfehler-Fix still wieder weg. Über die API setzt `{"ask": null}` das Häkchen
zurück auf „dem Quelltext folgen".

Für die Ausführung zählt immer: **Häkchen, sonst Quelltext.**

Die Markierung wird beim Speichern der Datei ausgelesen und als Flag in der
Datenbank festgeschrieben. Die Ausführung liest dieses Flag, nie den
Quelltext — sie kann sich zwischen Prüfung und Ausführung also nicht ändern.

### `allow-network:`

Ohne diese Markierung läuft der Container mit `--network none`: **kein**
Netzzugriff.

```python
# allow-network: api.open-meteo.com, geocoding-api.open-meteo.com
def aktuell(ort):
    ...
```

Erlaubt sind ausschließlich die aufgezählten Hosts, exakt geschrieben — keine
Wildcards, keine Suffix-Regel. `api.open-meteo.com` erlaubt nicht
`evil.api.open-meteo.com.angreifer.de`. Durchgesetzt wird das vom
Egress-Proxy außerhalb des Containers, nicht vom Code darin: ein roher Socket
im Skill kommt nicht weiter, weil es im Netz des Containers keinen Weg nach
draußen gibt.

Ports: nur 80 und 443.

Eine ganze URL wird toleriert und auf den Host reduziert
(`https://api.example.com/v1` → `api.example.com`).

### Beschreibung

Jede Kommentarzeile im Block, die keine Markierung ist, wird zur Beschreibung.
Sie steht im Freigabe-Dialog und im System-Prompt — bei einer
freigabepflichtigen Funktion ist sie das, woran der Nutzer die Entscheidung
festmacht. Ohne Kommentar nimmt Python die erste Zeile des Docstrings.

---

## 6. Skill-Variablen

API-Keys und Ähnliches gehören nicht in den Quelltext. Unter **Skill-Variablen**
im selben Panel werden beliebige Name/Wert-Paare hinterlegt (ein JSON-Objekt
pro Skill, Struktur frei).

In der Funktion stehen sie auf zwei Wegen bereit:

```python
import os

def abfragen():
    key = SKILL_VARS.get("API_KEY", "")          # Dict
    key = os.environ.get("API_KEY", "")          # Umgebungsvariable
```

```javascript
export async function abfragen() {
  const key = SKILL_VARS.API_KEY ?? process.env.API_KEY ?? '';
}
```

**Sichtbarkeit:** Werte sind nur für Admins schreibbar und **für niemanden
lesbar** — die Oberfläche zeigt ausschließlich Namen und eine Maske
(`****a1b2`). Ein leer gelassenes Feld lässt den gespeicherten Wert unberührt.
Werte erscheinen nicht in der Skill-Liste, nicht in der Antwort, nicht im
Audit-Log und nie im Prompt.

Namen: Buchstaben, Ziffern, `_`; nicht mit einer Ziffer beginnend. Maximal 64
Variablen, je 4096 Zeichen.

---

## 7. Rückgabewerte

Der Rückgabewert wird in den Antworttext eingesetzt:

| Rückgabe | Im Text |
|---|---|
| `"14 °C, bewölkt"` | `14 °C, bewölkt` |
| `21` | `21` |
| `True` / `False` | `ja` / `nein` |
| `None` | `—` |
| `{"temp": 14}` | `{"temp": 14}` (kompaktes JSON) |

**Gib fertigen Text zurück.** Ein Dict landet als JSON mitten im Satz — das
liest sich schlecht. `"14 °C, leicht bewölkt"` ist besser als
`{"temp": 14, "lage": "bewölkt"}`.

**Wiederhole nicht, was im Satz schon steht.** Das Modell schreibt den Ort
bereits davor („Aktuell in St. Pölten: …"). Gibt die Funktion ihn nochmal
zurück, liest sich die Antwort als „St. Pölten: St. Pölten (Niederösterreich):
20 °C". Der Wetter-Skill nennt den aufgelösten Ort deshalb nur noch, wenn er
vom gefragten **abweicht** — dann ist er keine Wiederholung, sondern die
Korrektur (`Frankfurt` → `Frankfurt am Main (Hessen)`), ohne die der Wert dem
falschen Ort zugeschrieben würde.

Das Ergebnis muss JSON-serialisierbar sein und darf 64 KB nicht überschreiten.

`print()` bzw. `console.log()` stören nicht — die Ausgabe wird abgefangen und
separat übergeben, nicht in die Antwort geschrieben.

---

## 8. Fehler

Eine Exception bricht **nicht** die Antwort ab. Statt des Ergebnisses erscheint
eine sichtbare Markierung:

```
Aktuell in Fürth: ⚠️ wetter.aktuell — Ort 'Fürth' wurde nicht gefunden.
```

Ein fehlgeschlagener Aufruf wird nie stillschweigend gelöscht: sonst bliebe ein
Satz stehen, der wie eine Tatsachenbehauptung liest, ohne dass etwas
dahintersteht.

Wirf `ValueError` mit einer verständlichen deutschen Meldung — sie landet
direkt vor den Augen des Nutzers.

---

## 9. Umgebung im Container

* **Python 3.11** mit `requests`. Sonst nichts vorinstalliert.
* **Node** mit `fetch` (durch den Proxy geleitet, wenn eine Allowlist gesetzt ist).
* Dateisystem **read-only**, außer `/tmp` (16 MB, `noexec`).
* **Zustandslos:** Jeder Aufruf startet einen frischen Container. Globale
  Variablen, Caches und geschriebene Dateien überleben keinen zweiten Aufruf.
* Standardlimits: **10 s**, **256 MB**, 0.5 CPU, 64 Prozesse. Obergrenzen per
  `.env` (`SKILL_MAX_TIMEOUT_MS`, `SKILL_MAX_MEMORY_MB`).
* Läuft als unprivilegierter Nutzer, ohne Capabilities.

Braucht eine Funktion mehr als das, gehört sie in einen Service — nicht in die
Sandbox.

---

## 10. Vollständiges Beispiel: der Wetter-Skill

Ausgeliefert in `services/evaluation/config/skills/wetter.py`, beim Start in
jeden aktiven Use Case geseedet.

```python
import os
import requests

_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

def _api_key_params():
    key = (os.environ.get("OPENMETEO_API_KEY") or "").strip()
    return {"apikey": key} if key else {}

def _geocode(ort):
    ...  # privat: Ortsname → Koordinaten

# Aktuelles Wetter für einen Ort. Liefert einen fertigen Satz.
# allow-network: geocoding-api.open-meteo.com, api.open-meteo.com
def aktuell(ort):
    lat, lon, label = _geocode(ort)
    response = requests.get(_FORECAST_URL, params={...}, timeout=8)
    response.raise_for_status()
    current = response.json()["current"]
    return f"{label}: {round(current['temperature_2m'])} °C, ..."
```

Zusammenspiel:

1. `detailed_task` weist das Modell an, bei Wetterfragen
   `wetter.aktuell("<Ort>")` zu schreiben und **keine Werte zu erfinden**.
2. Das Modell antwortet: `Aktuell in Nürnberg: wetter.aktuell("Nürnberg").`
3. Keine Freigabe nötig (reiner Lesezugriff) → der Aufruf läuft sofort.
4. Der Container darf genau die zwei Open-Meteo-Hosts erreichen.
5. Der Nutzer liest: `Aktuell in Nürnberg: Nürnberg (Bayern): 14 °C, ...`

---

## 11. Mehrere Skills nebeneinander

Ein Use Case kann beliebig viele Funktions-Skills tragen. Sie sind vollständig
unabhängig voneinander:

| | |
|---|---|
| **Sprache** | Pro Skill frei. Ein Python- und ein JavaScript-Skill laufen im selben Use Case nebeneinander. |
| **Variablen** | Jeder Skill hat sein eigenes JSON-Objekt. Ein Skill sieht **nur** seine eigenen Werte — weder über `SKILL_VARS` noch über die Umgebung. |
| **Netzzugriff** | Pro Funktion, nicht pro Use Case. Die Allowlist von Skill A öffnet für Skill B nichts, auch nicht während A gerade läuft. |
| **Freigabe** | Pro Funktion. Ein freigabepflichtiger Skill hält die anderen nicht auf. |
| **Fehler** | Pro Aufruf. Ein kaputter Skill kostet die Ergebnisse der anderen nicht. |

### Funktionsnamen dürfen sich wiederholen

Eindeutig ist immer `skill.funktion`, nie die Funktion allein. `wetter.aktuell`
und `klima.aktuell` sind zwei verschiedene Funktionen und stören einander
nicht. Nur der **Skill-Name** muss innerhalb eines Use Case eindeutig sein —
das erzwingt die Datenbank.

### Eine Antwort, mehrere Skills

```
Aktuell in Fürth: wetter.aktuell("Fürth"). Davon auf Lager:
lager.bestand("M8x40"), das macht brutto rechner.brutto(100).
```

Alle drei Aufrufe werden erkannt, einzeln aufgelöst und einzeln ersetzt.
Aufrufe **ohne** Freigabepflicht laufen dabei gleichzeitig — drei Funktionen
kosten also etwa so viel Zeit wie eine, nicht dreimal so viel. Freigabepflichtige
Aufrufe laufen einzeln, damit nie zwei Rückfragen gleichzeitig im Chat stehen.

Obergrenze: 8 Aufrufe pro Antwort, über alle Skills zusammen.

### Damit das Modell den richtigen Skill wählt

Die Funktionen erscheinen im Prompt nach Skill gruppiert, mit der **Übersicht**
des Skills als Überschrift. Diese Übersicht ist damit kein Beiwerk, sondern das,
woran das Modell entscheidet, welcher Skill zu einer Frage gehört. Bei mehreren
Skills lohnt es sich, sie scharf zu formulieren:

```
Gut:     Live-Wetterdaten über Open-Meteo
Schlecht: Hilfsfunktionen
```

Im `detailed_task` sollte stehen, *wann* der Skill greift — nicht nur, was er
kann. Je mehr Skills ein Use Case hat, desto wichtiger wird diese Abgrenzung.

---

## 12. Skills weitergeben: Export & Import

**Verwaltung → Skills → Exportieren** lädt alle Skills eines Use Case als eine
JSON-Datei (`skills_<use_case>.json`). Sie enthält Übersicht, Anweisungstext,
die vollständige Code-Datei und das Risikoprofil jeder Funktion.

**Variablenwerte sind absichtlich NICHT enthalten.** Eine Exportdatei landet im
Download-Ordner, wird per Mail verschickt und in Repositories eingecheckt — ein
Schlüssel darin ist ein verlorener Schlüssel. Exportiert werden nur die
**Namen**, damit die Gegenseite weiß, was noch zu befüllen ist:

```json
{
  "version": 1,
  "use_case": "ustp",
  "skills": [{
    "name": "wartung",
    "code_file_name": "wartung.py",
    "code": "…",
    "functions": [
      {"signature": "auftrag_anlegen(anlage)",
       "ask_for_permission": true,
       "allowed_domains": ["erp.example.com"]}
    ],
    "variable_names": ["ERP_TOKEN"]
  }]
}
```

**Importieren …** liest dieselbe Datei. Danach meldet die Oberfläche, was
tatsächlich angekommen ist — pro Skill die Funktionen, welche davon vor der
Ausführung nachfragen, welche Hosts sie erreichen dürfen und welche Variablen
noch keinen Wert haben. Ein Import bringt Code ins System, der später
ausgeführt wird; danach soll niemand suchen müssen, was er sich geholt hat.

Zwei Dinge sind beim Import wichtig:

- **Bestehende Skills werden übersprungen**, solange „Bestehende überschreiben"
  nicht aktiv ist. Ein zweiter Import verwirft damit keine lokalen Änderungen.
- **Flags werden neu aus dem Quelltext abgeleitet**, nicht aus der Datei
  übernommen. Ein Dokument, das `"ask_for_permission": false` behauptet,
  während die Funktion `# ask for permission` trägt, kann die Rückfrage nicht
  abschalten.
- **Beim Häkchen gilt eine Asymmetrie.** Es steht nicht im Quelltext, muss also
  im Dokument mitreisen — aber nur in eine Richtung: `permission_override: true`
  wird übernommen (eine bewusst gesetzte Rückfrage wird wiederhergestellt),
  `false` wird ignoriert. Sonst könnte eine von Hand geänderte Datei einem Skill
  die Rückfrage auf dem Transportweg abnehmen, und das ist die eine Richtung, in
  der ein Irrtum gefährlich ist.
- **Der Import meldet den tatsächlichen Zustand**, den er anschließend aus der
  Datenbank zurückliest — nicht das, was er geschrieben zu haben glaubt.

---

## 13. Wann das Modell ein Werkzeug findet — und wann nicht

Gemessen mit `ministral-3:14b` (14B, Ollama), je mehrere Läufe derselben Frage:

| Frageform | Beispiel | Skill wird gerufen |
|---|---|---|
| Ort und Absicht explizit | „wird es morgen in St. Pölten regnen?" | zuverlässig, ~8 s |
| Ort implizit, Absicht explizit | „regnet es morgen? ich muss zur FH." | zuverlässig, ~7 s |
| Beides implizit, Nebensache | „ich muss von Gebäude A zu B, brauche ich eine Jacke?" | **etwa 2 von 7**, 16–75 s |

Der dritte Fall ist die harte Grenze, und sie liegt beim Modell, nicht an der
Verdrahtung: Es muss selbst erkennen, dass „brauche ich eine Jacke" eine
Wetterfrage ist, und den Ort aus dem Kontext ergänzen. Ein 14B-Modell trifft
diese Kette nicht verlässlich.

Was hilft:

- **Skill-Variable `STANDARD_ORT`** (oder ein entsprechender Default im
  Skill-Code). Ohne sie rät das Modell — beobachtet wurden „Campus der TU
  München" und „Gebäude A" als Ortsangaben. Ein geratener Wert ist schlimmer
  als keine Antwort, weil er wie eine aussieht. Der Wetter-Skill bricht
  deshalb mit einer klaren Meldung ab, statt zu raten.
- **Den Ort im Use-Case-Prompt nennen.** Hilft auch der Dokumentensuche.
- **Ein `detailed_task`, der die Auslöser benennt**, nicht nur die Funktion:
  „Wetter, Regen, Jacke, draußen, Weg im Freien" statt nur „Wetterfragen".
- **Ein stärkeres Modell** für die Agent-Schritte. Das ist der eigentliche
  Hebel für Fall 3.

---

## 14. Checkliste

- [ ] Genau eine Datei, `.py` **oder** `.js`
- [ ] Aufrufbare Funktionen auf oberster Ebene, ohne `_`-Präfix
- [ ] Alles Schreibende/Kostende mit `ask for permission` markiert
- [ ] Markierungen kleben an der Definition (keine Leerzeile dazwischen)
- [ ] Netzzugriff per `allow-network:` auf die nötigen Hosts beschränkt
- [ ] Keine Secrets im Quelltext — Skill-Variablen benutzen
- [ ] Rückgabe ist lesbarer Text, nicht ein rohes Dict
- [ ] Fehler als `ValueError` mit verständlicher deutscher Meldung
- [ ] `detailed_task` sagt dem Modell, **wann** es aufrufen und dass es
      **nichts erfinden** soll
- [ ] Bei mehreren Skills im Use Case: Übersicht grenzt den Skill klar
      von den anderen ab
