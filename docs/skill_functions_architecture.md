# Architektur: Skill-Plugin-Layer

Erweiterung des bestehenden Skill-Systems um ausführbare Funktionsaufrufe. Ein
Skill bringt optional eine Code-Datei mit; das LLM referenziert deren
Funktionen im Antworttext, ein Parser erkennt die Aufrufe, und die Ausführung
läuft in einem kurzlebigen, isolierten Container.

Entwicklersicht: `docs/skill_functions_developer.md`.

---

## 1. Überblick

```
                  LLM-Antwort (Text)
                          │
                          ▼
   ┌──────────────────────────────────────────────────┐
   │ 1  Regex-Parser          shared/skills/call_parser│
   │    findet skillname.funktion(arg, …)             │
   └──────────────────────────────────────────────────┘
                          │
                          ▼
   ┌──────────────────────────────────────────────────┐
   │ 2  Lookup in der Skills-DB                       │
   │    usecase_skills ⋈ usecase_skill_functions      │
   │    nicht auflösbar ──► NICHTS ausführen, markieren│
   └──────────────────────────────────────────────────┘
                          │
          ┌───────────────┴────────────────┐
          │                                │
  ask_for_permission = false     ask_for_permission = true
          │                                │
          │                                ▼
          │                  ┌─────────────────────────────┐
          │                  │ 3  Rückfrage im Chat        │
          │                  │    Stream bleibt offen,     │
          │                  │    wartet auf asyncio.Event │
          │                  │    Timeout / keine Antwort  │
          │                  │    ──► abgelehnt            │
          │                  └─────────────────────────────┘
          │                                │ (bei Zustimmung)
          └───────────────┬────────────────┘
                          ▼
   ┌──────────────────────────────────────────────────┐
   │ 4  sandbox-Service      (einziger mit docker.sock)│
   │    docker run --rm  kermit-skill-runner           │
   │      --network none | --network skill_egress      │
   │      --read-only --cap-drop ALL --memory 256m     │
   │    Job über stdin, Ergebnis über stdout           │
   └──────────────────────────────────────────────────┘
                          │
                          ▼
   ┌──────────────────────────────────────────────────┐
   │ 5  Substitution: Aufruf-Span ► Ergebnis           │
   │    Fehler ► sichtbare ⚠️-Markierung               │
   └──────────────────────────────────────────────────┘
                          │
                          ▼
                 Antwort an den Nutzer
```

---

## 2. Datenbankschema

Aufbauend auf der bestehenden `usecase_skills`-Tabelle. Alle Erweiterungen
sind additiv und nullable — ein Skill ohne Funktionsteil verhält sich
unverändert.

Migration: `db/alembic/versions/0010_skill_functions.py`. Frischinstallation:
`db/init.sql`.

### `usecase_skills` (erweitert)

| Spalte | Typ | Zweck |
|---|---|---|
| `code_language` | `VARCHAR(20)` | `python` \| `javascript`, `CHECK`-constrained |
| `code_file_name` | `VARCHAR(255)` | Ursprünglicher Dateiname |
| `code` | `TEXT` | Die Code-Datei. Genau **eine** pro Skill |
| `variables` | `JSONB NOT NULL DEFAULT '{}'` | Dynamische Name/Wert-Paare |

`variables` ist bewusst schemalos: welche Werte ein Skill braucht, weiß nur
der Skill.

### `usecase_skill_functions` (neu)

Eine Zeile pro aufrufbarer Funktion, beim Speichern der Code-Datei aus dem
Quelltext abgeleitet.

| Spalte | Typ | Zweck |
|---|---|---|
| `skill_id` | `UUID` | FK → `usecase_skills`, `ON DELETE CASCADE` |
| `name`, `signature`, `description`, `params` | | Für Prompt und Freigabe-Dialog |
| `ask_for_permission` | `BOOLEAN` | Aus `# ask for permission` im Quelltext |
| `permission_override` | `BOOLEAN` | Wahl des Admins im Dashboard; `NULL` = dem Quelltext folgen. Effektiv gilt `COALESCE(override, ask_for_permission)` |
| `allowed_domains` | `JSONB` | Aus `# allow-network:`. Leer = kein Netz |
| `timeout_ms`, `memory_mb` | `INTEGER` | Ressourcenbudget, gedeckelt per `.env` |

> **Warum zwei Spalten für ein Häkchen?**
> Das Speichern der Code-Datei schreibt die Funktionszeilen aus dem Quelltext
> neu. Läge die Wahl des Admins in derselben Spalte, verschwände eine bewusst
> eingeschaltete Rückfrage beim nächsten Speichern — eine Sicherheitseinstellung,
> die still verloren geht, ist das eine Ergebnis, für das sich eine zusätzliche
> Spalte lohnt. `set_skill_code` trägt vorhandene Overrides über den Rewrite
> hinweg, und `SkillFunction.ask_for_permission` ist bereits der **effektive**
> Wert — Gate, Prompt und Sandbox müssen davon nichts wissen.

> **Warum die Flags in der DB und nicht im Quelltext?**
> Das Permission-Flag entscheidet, ob ein Mensch gefragt wird, bevor Code
> läuft. Würde es bei jeder Ausführung neu aus dem Quelltext gelesen, gäbe es
> ein Zeitfenster zwischen Prüfung und Ausführung, in dem sich die Markierung
> ändern kann. Die Zeile ist die Autorität; der Parser ist nur das, was sie
> schreibt. Beim Speichern werden die Zeilen **ersetzt**, nicht gemerged —
> eine aus der Datei entfernte Funktion muss aufhören, aufrufbar zu sein.

### `skill_executions` (neu)

Audit-Trail. Bewusst dünn: wer, was, wann, mit welchen Argumenten, ob eine
Freigabe nötig war und erteilt wurde, und wie es ausging. Das **Ergebnis**
steht nicht darin — nur seine Größe — und Skill-Variablen tauchen nirgends
auf.

`status ∈ {ok, error, denied, timeout, unresolved}`.

---

## 3. Regex-Erkennung

`shared/shared/skills/call_parser.py`

Der Parser läuft über fertige Modellausgabe und muss in beide Richtungen
vorsichtig sein: ein verpasster Aufruf steht als `wetter.aktuell(…)` in der
Antwort; ein übereifriger Treffer führt Code aus, den niemand angefordert hat.

Deshalb in drei Stufen:

1. **Kandidaten finden.** `(?<![\w.])(name)\.(name)\s*\(` — das Lookbehind
   schließt Property-Ketten (`obj.wetter.aktuell(`) aus.
2. **Skill-Filter.** Kandidaten, deren Skill der Use Case nicht kennt, fallen
   sofort raus. Das entfernt praktisch alle Fehltreffer aus Fließtext:
   `config.json(…)` oder `str.format(…)` benennen keinen Skill.
3. **Klammern zählen.** Ein echter Zeichen-Scan, der Anführungszeichen und
   Escapes respektiert — `wetter.aktuell("Frankfurt (Oder)")` schließt nicht
   zu früh. Balanciert der Ausdruck nicht, war es Prosa.

Zurück kommen exakte Zeichen-Spans, sodass die Substitution später nicht
erneut suchen muss. Umschließende Backticks werden mit einbezogen, damit
```` `wetter.aktuell("Fürth")` ```` keine Reste hinterlässt.

Die Substitution läuft **von rechts nach links**, damit frühere Offsets gültig
bleiben. Aufrufe ohne Ersetzung bleiben stehen.

Obergrenzen: 8 Aufrufe pro Antwort, 8 Argumente, 512 Zeichen je String.

---

## 4. Zwei Wege zu einer Funktion

### Der aktive Weg: die Action `SKILL` (Standard)

Der Agent ruft eine Funktion so auf, wie er sucht — als ReAct-Action:

```
ACTION: SKILL({"skill": "wetter", "function": "vorhersage", "args": ["St. Pölten", 1]})
OBSERVATION: 2026-09-15: 11 bis 24 °C, überwiegend klar
ACTION: FINAL_ANSWER({"answer": "Morgen wird es … (laut Skill wetter)."})
```

Entscheidend ist die **OBSERVATION**: Der Wert kommt zurück, *bevor* die
Antwort steht. Das Modell kann ihn deuten („keine Hinweise auf Regen"), mit
Dokumenttreffern zusammenführen und die Herkunft benennen.

`SKILL` ist genau dann verfügbar, wenn der Use Case aufrufbare Funktionen hat —
kein zweiter Schalter im Aktionen-Tab. Ein Werkzeug, das der Agent nicht
erreicht, existiert nicht, egal was die Skill-Seite zeigt. Aus demselben Grund
überlebt `SKILL` die Rollen-Filterung in `roles.py`: ein Sub-Task, der *für*
eine Live-Frage angelegt wurde, darf nicht ohne das eine Werkzeug ankommen,
das sie beantwortet.

### Der passive Weg: Textmuster in der Antwort (Rückfall)

Schreibt ein Modell den Aufruf trotzdem in den Antworttext
(`wetter.aktuell("Fürth")`), greift weiterhin der Regex-Parser und ersetzt ihn
durch das Ergebnis. Das bleibt als Auffangnetz erhalten, ist aber die
schwächere Variante: Zum Zeitpunkt der Ersetzung ist die Antwort geschrieben,
das Modell kann auf das Ergebnis nicht mehr reagieren.

Beide Wege laufen durch dieselbe Funktion (`_execute_one`) — Auflösung,
Freigabe-Gate, Sandbox-Aufruf und Audit-Zeile sind identisch. Zwei
Ausführungspfade mit je eigener Vorstellung davon, wann gefragt wird, sind der
Weg, auf dem einer davon irgendwann nicht mehr fragt.

### Die Zerlegung muss die Werkzeuge kennen

Der Decomposer bekommt die Funktionsliste in seinen Prompt, mit einer Regel:
*Ein Teil der Anfrage, den ein Werkzeug beantworten kann, MUSS einen eigenen
Sub-Task bekommen — auch wenn die Dokumente dazu nichts hergeben.*

Ohne das plante er rein dokumentenorientiert. Bei „ich fahre morgen in die FH,
wird es regnen? welchen Bus…" entstanden **zwei** Sub-Tasks, beide über Bus und
Gehweg — die Wetterfrage fiel beim Zerlegen weg. Danach kann kein Sub-Agent sie
mehr retten, weil es die Teilfrage nicht mehr gibt. Mit der Regel entstehen
drei Sub-Tasks, und der dritte ruft die Funktion auf.

## 5. Pipeline-Integration

`services/evaluation/core/skill_functions.py`, eingehängt in
`core/manager.py` (`run_manager`).

**Position: nach Compliance, vor Citation-Mapping.**

* *Nach* Compliance, weil Compliance prüft, was das **Modell** behauptet hat.
  Ein Funktionsergebnis ist abgerufene Information, keine Behauptung, die auf
  Halluzination zu prüfen wäre.
* *Vor* Citation-Mapping, damit `[n]`-Referenzen, `[BILD:]`-Marker und
  `answer_length` denselben Text sehen, den der Nutzer am Ende liest.

### Reihenfolge bei mehreren Aufrufen

Erst werden **alle** erkannten Aufrufe aufgelöst — gleichzeitig, denn die
Lookups sind unabhängig, und erst das Ergebnis sagt, welche Funktion eine
Freigabe braucht. Danach werden sie in Blöcke gruppiert:

* Aufrufe **ohne** Freigabepflicht laufen mit ihren Nachbarn **gleichzeitig**.
  Ein Use Case mit mehreren Skills produziert routinemäßig zwei bis drei
  Aufrufe pro Antwort; strikt nacheinander wären das Sekunden Wartezeit für
  Container, die auch zusammen hätten starten können.
* Ein Aufruf **mit** Freigabepflicht bildet einen Block für sich. Bei zwei
  offenen Freigabe-Dialogen in derselben Chat-Blase könnte niemand mehr sagen,
  welcher zu welchem Aufruf gehört — und die Antwort auf den ersten kann sehr
  wohl „nein" lauten.

Die Sandbox begrenzt die Gleichzeitigkeit zusätzlich serverseitig
(`SKILL_MAX_CONCURRENT`, Default 4).

Die gesamte Stufe ist in `try/except` gekapselt: Ist sie kaputt, bekommt der
Nutzer die unveränderte Antwort. Eine fertige Antwort darf nicht an der
Plugin-Schicht scheitern.

---

## 6. Permission-Flow

Der Chat ist ein einzelner streamender POST. Eine Rückfrage mitten in der
Antwort braucht also einen Seitenkanal.

```
evaluation                          Frontend
    │                                   │
    │──{"type":"permission_request",    │
    │   "call_id":"…","skill":"…"}─────►│  Dialog mit Erlauben/Ablehnen
    │                                   │
    ⏸  await asyncio.Event  (120 s)     │
    │                                   │
    │◄── POST /v1/agent/permission/{id} │  {"approved":true,"session_id":"…"}
    │                                   │
    │──{"type":"permission_resolved"}──►│
    │──{"type":"function_result"}──────►│
    │──{"type":"final", …}─────────────►│
```

`core/permissions.py` ist die Rendezvous-Stelle: ein In-Process-Dict aus
`call_id` → `asyncio.Event`.

**Bewusst nicht persistent.** Eine offene Anfrage ist nur sinnvoll, solange der
Stream lebt, der sie gestellt hat. Nach einem Neustart wäre eine gespeicherte
Freigabe eine Freigabe, auf die niemand mehr wartet. Was überdauern soll,
steht in `skill_executions`.

**Ablehnung ist der Default in jedem unklaren Fall:** Timeout, geschlossener
Tab, Prozess-Neustart, oder gar kein Event-Kanal (die nicht-streamende
öffentliche API). Nichts läuft, weil jemand nicht geantwortet hat.

Die `session_id` wird gegengeprüft — sonst könnte ein Client durch Raten einer
`call_id` eine Ausführung in einer fremden Unterhaltung freigeben.

### Isolation zwischen Skills

Ein Use Case trägt beliebig viele Funktions-Skills. Dass sie einander nicht
sehen, folgt nicht aus einer Prüfung, sondern aus der Bauform — ein Container
pro Aufruf, bestückt mit genau den Daten dieses einen Aufrufs:

| | |
|---|---|
| **Variablen** | Nur die des eigenen Skills werden in den Container gereicht. Ein anderer Skill hat weder über `SKILL_VARS` noch über die Umgebung Zugriff. |
| **Netz** | Der Egress-Grant hängt an der **Ausführung**, nicht am Skill. Ein gleichzeitig laufender Skill ohne Allowlist bekommt kein Proxy-Token und damit keine Route nach draußen. |
| **Zustand** | Kein gemeinsames Dateisystem, kein Pool. Zwei Skills können einander nicht beobachten, auch nicht zeitgleich. |
| **Sprache** | Reist pro Aufruf mit. Python- und JavaScript-Skills laufen im selben Use Case nebeneinander; das Runner-Image trägt beide Laufzeiten. |

Eindeutig ist immer `skill.funktion`: Zwei Skills dürfen gleichnamige
Funktionen haben. Die Datenbank erzwingt Eindeutigkeit nur für
`(use_case, skill_name)` und `(skill_id, function_name)`.

### Die zweite Quellenart

Die Plattform bindet Antworten strikt an Dokumente: der ReAct-Suffix verbietet
jeden Wert, der nicht in einem OBSERVATION-Chunk steht („ABSOLUTES VERBOT"),
und der Compliance-Agent verwirft Antworten ohne `[n]`-Beleg. Ein
Funktionsergebnis ist genau so ein Wert — ohne Anpassung schreibt kein Modell
je einen Aufruf, und tut es doch eines, wird die Antwort verworfen.

Aufgelöst wird das **nicht** durch eine Ausnahme vom Belegzwang, sondern durch
eine zweite zulässige Quellenart — im selben Muster, in dem die Datei schon
Bildinhalte behandelt:

| | Dokument | Skill-Funktion |
|---|---|---|
| Beleg | `[n]` auf einen Chunk | der Aufruf selbst |
| Quellenangabe im Satz | optional | **„laut Skill wetter"** |
| Prüfung | Chunk enthält die Aussage | Aufruf ist ein Platzhalter |

Die Belegpflicht wird dadurch nicht schwächer: Es gibt weiterhin keine
unbelegte Aussage, die Quelle heisst nur anders. Betroffen sind drei Stellen:

1. **`core/prompts/agent_react_suffix.txt`** — Abschnitt
   `AUSFUEHRBARE SKILL-FUNKTIONEN`. Er stellt ausserdem klar, dass ein Aufruf
   **keine ACTION** ist, sondern in den Text der `FINAL_ANSWER` gehört: ohne
   diesen Satz emittiert das Modell `ACTION: wetter.aktuell(…)`, was der
   ReAct-Parser nicht auflösen kann.
2. **Reihenfolge im System-Prompt** — der Funktionsblock steht **hinter** dem
   Suffix. Davor war das „ABSOLUTES VERBOT" das Letzte, was das Modell las.
3. **Compliance** (`core/manager.py`) — der deterministische
   Inline-Citation-Check lässt Antworten mit erkanntem Aufruf durch, und der
   Compliance-Prompt kennt Aufrufe als Platzhalter. Beanstandet wird dort nur
   noch der eine echte Fehler: ein **ausgeschriebener** Wert statt des Aufrufs.

### Die Auto-Korrekturen des Agenten

`run_agent` überstimmt das Modell an zwei Stellen deterministisch: es erzwingt
eine erste `SEARCH`, bevor überhaupt geantwortet werden darf, und eine zweite,
breitere Suche, bevor „keine Information" akzeptiert wird. Beide existieren,
weil kleine Modelle sonst aus dem Gesprächsverlauf heraus antworten.

Genau diese Guards haben den Plugin-Layer blockiert. Im Protokoll eines echten
Laufs (`ministral-3:14b`) steht zweimal wörtlich:

> THOUGHT: … Die Skill-Funktion `wetter.aktuell` ist hier die passende Quelle.
> `[Auto-Korrektur: SEARCH erzwungen, bevor eine Antwort gegeben wird.]`

Das Modell wollte die Funktion nutzen und wurde zweimal überstimmt; im dritten
Schritt gab es auf. Beide Guards prüfen deshalb jetzt, ob die Antwort einen
**auflösbaren** Skill-Aufruf enthält (`find_calls` gegen die Skills des Use
Case). Wenn ja, ist sie keine aus der Luft gegriffene Antwort, sondern eine
Anforderung von Live-Daten — und wird durchgelassen. Aus demselben Grund gilt
eine solche Antwort als `sufficient`, obwohl sie sagt, dass die Dokumente
nichts hergeben.

Ebenso entkoppelt: Die Funktionsregel hängt **nicht** daran, dass die Suche
nichts findet. Der Normalfall ist, dass SEARCH Unpassendes liefert — dann griff
vorher die Regel „sage, dass die Dokumente nichts enthalten", bevor eine
Funktion überhaupt in Betracht kam.

---

## 7. Sandbox

### Warum ein eigener Service

Der Docker-Socket ist root-äquivalent auf dem Host. Er liegt deshalb im
`sandbox`-Service und **nicht** im `evaluation`-Service, der LLM-Ausgaben
verarbeitet. `evaluation` spricht nur HTTP (`SANDBOX_TOKEN` als gemeinsames
Geheimnis) und startet nie selbst einen Container.

Der Sandbox-Service kennt bewusst keine *Policy*: er weiß nicht, was ein Skill
ist, liest die Datenbank nicht und entscheidet nie, ob eine Funktion laufen
darf. Berechtigung und Allowlist kommen vom Aufrufer aus den beim Import
geschriebenen Zeilen.

### Der Runner-Container

Ein Container **pro Aufruf**, `--rm`, Job über stdin, Ergebnis über stdout.
Kein Pool, kein warmer Container, kein geteiltes Dateisystem — so, dass zwei
Ausführungen einander nicht beobachten können.

| Flag | Wirkung |
|---|---|
| `--network none` | Default. Kein Netz, außer die Funktion hat Hosts deklariert |
| `--read-only`, `--tmpfs /tmp:noexec,size=16m` | Nichts überlebt den Aufruf |
| `--cap-drop ALL`, `--security-opt no-new-privileges` | Keine Capabilities, kein Zurückgewinnen |
| `--memory` + `--memory-swap` (gleich) | Ohne Swap-Limit liefe die Grenze ins Leere |
| `--cpus`, `--pids-limit` | Amoklauf trifft den Container, nicht den Host |
| `--user 10001` | Nie root, auch nicht im Container |

Der Entry-Point führt **ausschließlich die eine benannte Funktion** aus. Der
Modulrumpf läuft (Imports, Konstanten leben dort), aber keine andere Funktion
der Datei wird aufgerufen, und es gibt keinen Weg vom Job zu einer Shell.

> Weder `exec()` mit beschnittenen Builtins noch Nodes `vm` sind
> Sicherheitsgrenzen — beides ist bekanntermaßen ausbrechbar. Die Grenze ist
> der Container. Die Runtime absichtlich nicht zu beschneiden, ist ehrlicher
> als eine Einschränkung, die Sicherheit nur suggeriert.

`print()`/`console.log()` werden abgefangen: stdout gehört dem
Ergebnisprotokoll.

### Netz-Allowlist

Ein Container mit deklarierten Hosts hängt in `kermit_skill_egress`, einem
Docker-Netz mit `internal: true` — **ohne Route nach draußen**. Der einzige
erreichbare Peer ist der Egress-Proxy im Sandbox-Service. Durchgesetzt wird
also außerhalb des Gastprozesses: ein roher Socket im Skill hat kein Ziel.

```
Runner ──HTTP_PROXY/HTTPS_PROXY──► Egress-Proxy ──► erlaubter Host
 (kermit_skill_egress, internal)    (sandbox)        (default-Netz)
```

Der Proxy vergibt pro **Ausführung** ein Zufalls-Token, das dem Container als
Proxy-Credential mitgegeben und beim Beenden widerrufen wird. Zwei Funktionen
desselben Skills mit unterschiedlichen Allowlists teilen sich also nie eine
Egress-Policy, und ein geleaktes Token ist nach dem Aufruf wertlos.

Host-Vergleich ist **exakt**, nicht als Suffix: `api.open-meteo.com` erlaubt
nicht `api.open-meteo.com.angreifer.de`. Ports: nur 80 und 443. Unterstützt
werden `CONNECT` (HTTPS) und Absolute-Form-`GET` (HTTP).

Nodes eingebautes `fetch` ignoriert `HTTP_PROXY` — der JS-Entry-Point
installiert deshalb explizit einen undici-`ProxyAgent`. Ohne das würde ein
JS-Skill mit Allowlist stillschweigend direkt verbinden und scheitern.

---

## 8. Zugriffsschutz für Variablen

Kein Feld-Level-Encryption (außerhalb des Auftragsumfangs), sondern
Zugriffsbeschränkung auf drei Ebenen:

1. **Nicht im Datenmodell der Liste.** `Skill` (`resolver.py`) hat kein
   `variables`-Feld. Kein Listing kann sie versehentlich mitsenden.
2. **Nur über Admin-Endpoints.** `/v1/admin/skills/item/{id}/variables` liegt
   unter `/api/admin`, das `hooks.server.ts` auf `role=admin` beschränkt.
3. **Lesen ist maskiert.** Auch der Admin bekommt nur Namen und `****a1b2`.
   Ein `null`-Wert beim Schreiben heißt „unverändert lassen" — sonst würde ein
   Admin, der eine Variable bearbeitet, alle anderen mit ihrer eigenen Maske
   überschreiben.

Zur Laufzeit wandern die Werte in einem Zug von der DB in den Sandbox-Aufruf
und werden dort verworfen. Sie stehen nicht im Prompt, nicht in der Antwort,
nicht im Trace und nicht im Audit-Log.

---

## 9. Fehlerverhalten

| Fall | Ergebnis |
|---|---|
| Skill/Funktion nicht auflösbar | `unresolved` — **keine** Ausführung, ⚠️ im Text |
| Freigabe verweigert / Timeout | `denied` — keine Ausführung |
| Zeitlimit überschritten | `timeout` — Container wird zwangsweise entfernt |
| Speicherlimit (Exit 137) | `error` mit benannter Ursache |
| Sandbox nicht erreichbar | `error`, Antwort bleibt erhalten |
| Unerwartete Exception | `error`, Antwort bleibt erhalten |

Ein fehlgeschlagener Aufruf wird **nie stillschweigend entfernt**: sonst bliebe
ein Satz stehen, der wie eine belegte Aussage klingt, ohne dass etwas
dahintersteht.

---

## 10. Dateien

| Pfad | Rolle |
|---|---|
| `shared/shared/skills/code_parser.py` | Import: Quelltext → Funktionstabelle |
| `shared/shared/skills/call_parser.py` | Erkennung von Aufrufen im Antworttext |
| `shared/shared/usecase_config/resolver.py` | DB-Zugriff, Cache, Audit |
| `services/evaluation/core/skill_functions.py` | Pipeline-Stufe |
| `services/evaluation/core/permissions.py` | Freigabe-Rendezvous |
| `services/evaluation/core/skill_seed.py` | Seeding aus `config/skills/` |
| `services/sandbox/main.py` | HTTP-API des Sandbox-Service |
| `services/sandbox/runner.py` | `docker run` pro Aufruf |
| `services/sandbox/proxy.py` | Egress-Proxy mit Per-Call-Allowlist |
| `services/sandbox/runner/` | Runner-Image + Entry-Points (py/js) |
| `db/alembic/versions/0010_skill_functions.py` | Migration |
| `db/migrate.py` | Stempelt eine aus `init.sql` erzeugte DB und migriert sie (`make migrate`) |
| `core/prompts/agent_react_suffix.txt` | Skill als zweite Quellenart |

## 11. Konfiguration

Alle Variablen mit Erläuterung in `.env.example`, Abschnitt
„Skill-Plugin-Layer". Die wichtigsten:

| Variable | Default | Bedeutung |
|---|---|---|
| `SKILL_FUNCTIONS_ENABLED` | `true` | Master-Schalter; `false` = Parser läuft nicht |
| `SANDBOX_TOKEN` | — | **In Produktion setzen.** Ohne ihn kann jeder Prozess im internen Netz Code ausführen lassen |
| `SKILL_MAX_TIMEOUT_MS` | `30000` | Obergrenze über allen Funktionswerten |
| `SKILL_MAX_MEMORY_MB` | `512` | dito |
| `SKILL_MAX_CONCURRENT` | `4` | Gleichzeitige Runner-Container |
| `SKILL_PERMISSION_TIMEOUT_S` | `120` | Frist der Rückfrage; danach abgelehnt |

Das Runner-Image baut `make skill-runner` (in `make up` und `make dev`
enthalten).

## 12. Nicht enthalten

* Verschachtelte Aufrufe (Funktion ruft andere Skill-Funktion)
* Weitere Sprachen über Python/JavaScript hinaus
* Feld-Level-Verschlüsselung der Variablen (nur Zugriffsbeschränkung)
* Monitoring-Dashboard — `skill_executions` liefert die Daten,
  `GET /v1/admin/skills/{use_case}/executions` die Schnittstelle
