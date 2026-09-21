"""Referenz-Skill: aktuelle Wetterdaten über Open-Meteo.

Beispiel-Implementierung des Plugin-Layers, produktiv in der Pipeline.
Bewusst als reiner Lesezugriff ausgelegt — keine Funktion trägt hier
``# ask for permission``, weil eine öffentliche Wetterabfrage nichts
verändert und nichts preisgibt. Jede Funktion, die schreibt, Kosten
verursacht oder personenbezogene Daten berührt, muss die Markierung tragen.

Aufruf im LLM-Output::

    wetter.aktuell("Nürnberg")
    wetter.vorhersage("Nürnberg", 3)

Netzzugriff: Der ``# allow-network:``-Kommentar direkt über einer Funktion ist
die einzige Stelle, an der Hosts freigegeben werden. Alles, was dort nicht
steht, ist im Container nicht erreichbar — nicht, weil der Code es nicht
versucht, sondern weil der Egress-Proxy die Verbindung ablehnt.

Variablen: ``OPENMETEO_API_KEY`` ist optional. Die kostenlose Open-Meteo-API
braucht keinen Schlüssel; die kommerzielle schon. Der Wert kommt aus den
Skill-Variablen (Verwaltung → Skills → Skill-Variablen) und wird nie in einer
Antwort oder im Audit-Log sichtbar.
"""

import os
import re

import requests

_GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

_TIMEOUT = 8

# WMO-Wettercodes in lesbaren Text. Open-Meteo liefert nur die Zahl, und "3"
# ist in einer Antwort für niemanden hilfreich.
_WETTERCODES = {
    0: "klar", 1: "überwiegend klar", 2: "teilweise bewölkt", 3: "bedeckt",
    45: "Nebel", 48: "gefrierender Nebel",
    51: "leichter Nieselregen", 53: "Nieselregen", 55: "starker Nieselregen",
    61: "leichter Regen", 63: "Regen", 65: "starker Regen",
    71: "leichter Schneefall", 73: "Schneefall", 75: "starker Schneefall",
    77: "Schneegriesel",
    80: "leichte Schauer", 81: "Schauer", 82: "kräftige Schauer",
    85: "leichte Schneeschauer", 86: "starke Schneeschauer",
    95: "Gewitter", 96: "Gewitter mit leichtem Hagel", 99: "Gewitter mit Hagel",
}


def _aufloesen(ort):
    """Den abzufragenden Ort bestimmen — oder klar sagen, dass keiner feststeht.

    Nennt die Frage keinen Ort, raet ein Modell einen: beobachtet wurde eine
    Abfrage fuer „Campus der TU München" in einem Use Case, der an der FH
    St. Pölten haengt. Ein geratener Ort ist schlimmer als keine Antwort — er
    sieht aus wie eine.

    Deshalb: Ein hinterlegter STANDARD_ORT (Skill-Variable) gilt, wenn die
    Frage selbst keinen brauchbaren Ort liefert. Ohne beides bricht die
    Funktion mit einer Meldung ab, die dem Agenten sagt, was zu tun ist.
    """
    kandidat = (ort or "").strip()
    # Platzhalter, die trotz aller Prompt-Regeln durchkommen.
    if re.fullmatch(r"[\[<(]?\s*(standort|ort|stadt|name|hier|x|y)\s*[\]>)]?",
                    kandidat, re.IGNORECASE):
        kandidat = ""
    standard = (os.environ.get("STANDARD_ORT") or "").strip()
    if not kandidat:
        if standard:
            return standard
        raise ValueError(
            "Kein Ort angegeben. Nenne den Ort aus der Frage des Nutzers, oder "
            "hinterlege die Skill-Variable STANDARD_ORT. Rate niemals einen Ort."
        )
    return kandidat


def _api_key_params():
    """Commercial Open-Meteo keys ride along as a query parameter."""
    key = (os.environ.get("OPENMETEO_API_KEY") or "").strip()
    return {"apikey": key} if key else {}


def _klarstellung(ort, label):
    """Der aufgeloeste Ortsname – aber nur, wenn er vom Gefragten abweicht.

    Das Modell schreibt den Ort bereits in seinen Satz. Ihn hier zu wiederholen
    ergab "St. Pölten: St. Pölten (Niederösterreich): 20 °C". Weicht der
    gefundene Ort dagegen ab, ist er keine Wiederholung, sondern die Korrektur,
    ohne die der Wert dem falschen Ort zugeschrieben wuerde.
    """
    gefragt = (ort or "").strip().lower().replace(".", "").replace(" ", "")
    gefunden = label.split("(")[0].strip().lower().replace(".", "").replace(" ", "")
    return f"{label}: " if gefragt != gefunden else ""


def _geocode(ort):
    """Ortsname → (lat, lon, Anzeigename). Wirft bei unbekanntem Ort."""
    response = requests.get(
        _GEOCODING_URL,
        params={"name": ort, "count": 1, "language": "de", "format": "json",
                **_api_key_params()},
        timeout=_TIMEOUT,
    )
    response.raise_for_status()
    results = (response.json() or {}).get("results") or []
    if not results:
        raise ValueError(f"Ort '{ort}' wurde nicht gefunden.")
    top = results[0]
    label = top["name"]
    if top.get("admin1") and top["admin1"] != top["name"]:
        label = f"{top['name']} ({top['admin1']})"
    return top["latitude"], top["longitude"], label


# Aktuelles Wetter für einen Ort. Liefert einen fertigen Satz.
# allow-network: geocoding-api.open-meteo.com, api.open-meteo.com
def aktuell(ort):
    ort = _aufloesen(ort)
    lat, lon, label = _geocode(ort)
    response = requests.get(
        _FORECAST_URL,
        params={
            "latitude": lat,
            "longitude": lon,
            "current": "temperature_2m,relative_humidity_2m,wind_speed_10m,weather_code",
            "timezone": "auto",
            **_api_key_params(),
        },
        timeout=_TIMEOUT,
    )
    response.raise_for_status()
    current = (response.json() or {}).get("current") or {}
    if "temperature_2m" not in current:
        raise ValueError(f"Keine aktuellen Messwerte für {label} verfügbar.")

    beschreibung = _WETTERCODES.get(current.get("weather_code"), "unbestimmt")
    return (
        f"{_klarstellung(ort, label)}{round(current['temperature_2m'])} °C, "
        f"{beschreibung}, "
        f"Luftfeuchte {round(current.get('relative_humidity_2m', 0))} %, "
        f"Wind {round(current.get('wind_speed_10m', 0))} km/h"
    )


# Vorhersage für die nächsten Tage (1–7). Liefert einen Satz pro Tag.
# allow-network: geocoding-api.open-meteo.com, api.open-meteo.com
def vorhersage(ort, tage=3):
    try:
        tage = max(1, min(int(tage), 7))
    except (TypeError, ValueError):
        tage = 3

    ort = _aufloesen(ort)
    lat, lon, label = _geocode(ort)
    response = requests.get(
        _FORECAST_URL,
        params={
            "latitude": lat,
            "longitude": lon,
            "daily": "temperature_2m_max,temperature_2m_min,weather_code",
            "forecast_days": tage,
            "timezone": "auto",
            **_api_key_params(),
        },
        timeout=_TIMEOUT,
    )
    response.raise_for_status()
    daily = (response.json() or {}).get("daily") or {}
    dates = daily.get("time") or []
    if not dates:
        raise ValueError(f"Keine Vorhersage für {label} verfügbar.")

    zeilen = []
    for i, datum in enumerate(dates):
        beschreibung = _WETTERCODES.get(daily["weather_code"][i], "unbestimmt")
        zeilen.append(
            f"{datum}: {round(daily['temperature_2m_min'][i])} bis "
            f"{round(daily['temperature_2m_max'][i])} °C, {beschreibung}"
        )
    praefix = _klarstellung(ort, label)
    return (praefix.rstrip(": ") + " — " if praefix else "") + "; ".join(zeilen)
