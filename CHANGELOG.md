# myhems Changelog

## v0.8.0 (Mai 2026)
- min_soc aufgeteilt in min_soc_aus / min_soc_ein (Hysterese, ~5% Gap)
  - Abschalten wenn SOC < min_soc_aus, Freigabe erst wieder bei SOC >= min_soc_ein
  - Muddl: min_soc_aus=90%, min_soc_ein=95%
  - Udo: min_soc_aus=80%, min_soc_ein=85%
  - Neue State-Variable _soc_sperre, im Dashboard als Warnung angezeigt
- Netz als zusätzliches Schaltsignal (gleiche Schwellen wie Marstek)
  - Hochschalten bei Netzeinspeisung > hochschalten_schwelle
  - Runterschalten bei Netzbezug > runterschalten_schwelle
- SOC-Fehler: kein Hochschalten solange SOC unbekannt, außer Netzeinspeisung > Schwelle bestätigt
- Dashboard: SOC-Balken zeigt zwei Markierungen (AUS=rot, EIN=orange), SOC-Sperre-Banner
- API: MIN_SOC_AUS + MIN_SOC_EIN statt MIN_SOC in params

## v0.7.0 (April 2026)
- Regelparameter umbenannt: lade_schwelle → hochschalten_schwelle, entlade_schwelle → runterschalten_schwelle
- hysterese-Parameter entfernt – Abstand zwischen Schwellen übernimmt diese Rolle
- min_pv-Parameter entfernt – redundant da Marstek autonom auf Nulleinspeisung regelt
- SOC-Schutz: stufenweise Abschaltung mit delay statt Sofortabschaltung auf 0
- config_udo.yaml: hochschalten_schwelle=1100W, runterschalten_schwelle=1100W
- config_muddl.yaml: hochschalten_schwelle=300W, runterschalten_schwelle=300W

## v0.6.0 (April 2026)
- Tagesenergie-Feature: PV, Einspeisung, Netzbezug, Eigenverbrauch (heute + gestern)
- Akkumulierung per Poll-Zyklus (Watt → Wh), persistiert in energy_history.json
- Eigenverbrauch wird live berechnet (PV − Einspeisung), nicht gespeichert
- JSON rollierend: immer nur heute + gestern, wächst nie
- Dashboard: neuer Bereich "Tagesenergie" unterhalb Regelstatus
- config_udo.yaml: min_soc von 30 auf 80 erhöht

## v0.5.4 (April 2026)
- Aktueller Stand auf hemsbox-udo und hemsbox-muddl

## v0.5.2 (April 2026)
- Aktueller Stand auf hemsbox-udo

## Offene TODOs
- [ ] Responsive Dashboard Layout (CSS media queries für PC-Browser)
- [ ] App-Icon PNG (192×192, 512×512)
- [ ] Muddl: Cloudflare Tunnel
- [ ] PV Forecast (Open-Meteo Integration)
