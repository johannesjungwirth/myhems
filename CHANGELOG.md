# myhems Changelog

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
- [ ] Standort-Label im Header (Wort "Standort" entfernen)
- [ ] App-Icon PNG (192×192, 512×512)
- [ ] Muddl: Cloudflare Tunnel
