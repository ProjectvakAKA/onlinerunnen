# Metrics voor accuraatheid en juistheid

Dit document beschrijft welke metrics het systeem bijhoudt en hoe je accuraatheid beter kunt meten en vergelijken.

---

## 1. Wat er nu in de CSV staat (verwerking_log.csv)

Na elke verwerking wordt een regel toegevoegd met o.a.:

| Kolom | Betekenis |
|-------|-----------|
| **timestamp** | Wanneer verwerkt |
| **filename** | Naam van het PDF-bestand |
| **document_type** | Huurovereenkomst / EPC / etc. |
| **confidence_score** | Score 0–100 (compleetheid + kritieke velden + tekstlengte) |
| **needs_review** | Ja/nee (o.a. score < drempel of ontbrekende kritieke velden) |
| **text_length** | Aantal tekens geëxtraheerde tekst |
| **fields_complete** | % velden ingevuld (compleetheid) |
| **source_quote_pct** | % velden mét bronvermelding (source_quote) – hoe controleerbaar |
| **extracted_huurprijs** | Uitgelezen huurprijs (zoals opgeslagen) |
| **extracted_adres** | Uitgelezen pandadres |
| **extracted_ingangsdatum** | Uitgelezen ingangsdatum |
| **extracted_verhuurder** | Uitgelezen verhuurdernaam |
| **extracted_huurder** | Uitgelezen huurdernaam |
| **issues** | Ontbrekende kritieke velden e.d. |
| **warnings** | Korte tekst, etc. |
| **json_path** | Naam van het opgeslagen JSON-bestand |
| **processing_status** | success / etc. |

**Vergelijken:** Open de CSV naast het originele PDF (of een Excel met handmatig ingevulde “correcte” waarden). Vergelijk de kolommen `extracted_*` met wat er in het document staat. Zo zie je per document of huurprijs, adres, datum en partijen kloppen.

**Let op:** Als je al een oude `verwerking_log.csv` hebt (zonder de nieuwe kolommen), schakelt het systeem **automatisch** over op een nieuw bestand: **`verwerking_log_v2.csv`**. De oude log blijft ongewijzigd; nieuwe regels komen in v2. Je hoeft dus niets handmatig te hernoemen of verwijderen.

---

## 2. Betere manieren om accuraatheid te meten

### A) Handmatige steekproef (met de nieuwe CSV)

- Pak bijvoorbeeld 10–20 contracten.
- Open elk PDF en noteer de “echte” waarden voor huurprijs, adres, ingangsdatum, verhuurder, huurder.
- Zet die in een spreadsheet naast de CSV-regels (of in extra kolommen “verwacht_huurprijs”, …).
- Tel per veld: aantal keer correct / totaal → **veld-accuraatheid**.
- Gemiddelde over velden of over documenten → **algemene accuraatheid**.

### B) Gold set (referentiedata)

- Kies een vaste set documenten (bijv. 20–50) en vul handmatig de “correcte” JSON in (of een eenvoudige tabel met de 5 kritieke velden).
- Na elke wijziging aan prompts of normalizer: run de parser op de gold set en vergelijk output vs. referentie.
- Metrics: per veld **precision/recall** of **exact match %**; eventueel genormaliseerde match (bv. 2300 = 2.300).

Dit vereist een klein script dat:
- de gold JSON (of CSV) inleest,
- de geparseerde output inleest (bijv. uit Supabase of uit lokaal opgeslagen JSON),
- per veld vergelijkt en een match-% uitrekent.

### C) Feedback uit AI-goedkeuring

- In de app: gebruiker klikt “Ja” (accepteren) of “Aanpassen” (wijzigen).
- Als je per veld logt: `document_id`, `veld`, `waarde_extracted`, `waarde_na_actie`, `actie` (accepted / edited), kun je later uitrekenen:
  - **Acceptatieratio** per veld: % velden die zonder wijziging worden goedgekeurd.
  - **Meest gewijzigde velden** → prioriteit voor verbetering (prompts, normalizer).

Dit vergt in de frontend of API een kleine log/event wanneer “Ja” of “Opslaan” na aanpassen wordt geklikt, en opslag (bijv. in Supabase of een aparte tabel).

### D) Interne consistentie

- Als je meerdere documenten per pand hebt (huur + EPC + kadaster): controleer of adres en eventueel andere velden overeenkomen tussen die documenten.
- Metric: % document-paren voor hetzelfde adres waar de overlappende velden consistent zijn.

---

## 3. Wat de confidence-score wél en niet is

- **Wat het wél is:** een maat voor **compleetheid** en **type-verificatie** (zijn kritieke velden aanwezig, hoe veel % van de velden is ingevuld, lengte tekst).
- **Wat het niet is:** een directe maat voor **accuraatheid** (klopt de huurprijs met het document?). Een hoog score betekent niet automatisch dat alle waarden correct zijn.

Daarom zijn de nieuwe CSV-kolommen (`extracted_*` en `source_quote_pct`) nuttig: ze maken **vergelijking met het origineel** en **controleerbaarheid** expliciet.

---

## 4. Korte checklist voor verbetering

1. **Vergelijken:** Gebruik de CSV met `extracted_*` naast het PDF of een handmatige tabel; noteer fouten per veld.
2. **Bronvermelding:** Kijk naar `source_quote_pct`; lage waarden = weinig velden met bron, lastiger te controleren.
3. **Issues/warnings:** Gebruik `issues` en `warnings` om systematische problemen (ontbrekende velden, korte teksten) te vinden.
4. **Gold set (optioneel):** Bouw een kleine set met referentiewaarden en vergelijk na elke wijziging.
5. **Feedback (optioneel):** Log acceptatie vs. aanpassen in AI-goedkeuring om per veld te zien waar gebruikers het vaakst corrigeren.
