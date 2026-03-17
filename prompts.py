# -*- coding: utf-8 -*-
"""
Alle AI-prompts voor contract_system.py.
Bewerk hier de teksten; ze worden door contract_system geïmporteerd en gebruikt.

Placeholders in templates (worden door contract_system ingevuld):
- PROMPT_ORGANIZE: filename, current_location, pages_scanned, total_pages, method_note, existing_folders, text_for_call
- PROMPT_PARTIJEN / PAND / FINANCIEEL / etc.: text_chunk_1, text_chunk_2, source_quote_instruction
- PROMPT_SUMMARY: doc_type, text_sample
"""

# =============================================================================
# OCR / VISION
# =============================================================================

PROMPT_OCR_VISION = """Extract ALL text from these images. Include handwritten text if present. Return ONLY the extracted text, no explanations."""


# =============================================================================
# ORGANISEREN (classificatie + samenvatting in één call)
# =============================================================================

PROMPT_ORGANIZE = """SYSTEM: Je bent een EXPERT documentclassificeerder. Je doet TWEE dingen in één antwoord:
1) LEES het document en maak een UITGEBREIDE samenvatting van ongeveer 300 woorden (korter alleen als er niet genoeg relevante informatie in het document staat). Richt je op alle inhoudelijk relevante punten: partijen, adres, bedragen, termijnen, bijzondere bepalingen, etc.
2) BEPAAL het type document en de mapstructuur (waar het in Dropbox moet).

CRITICAL: /Onbekend_adres is ALLEEN voor CONTRACTEN (huur, EPC, eigendomstitel, …) waar het adres niet uit de tekst komt. Verhalen, essays, onderwijs, facturen → NOOIT /Onbekend_adres. Verhaal/essay/narratief → ALTIJD /Verhaal.

FILENAME: {filename}
LOCATION: {current_location}
EXTRACTION: {pages_scanned}/{total_pages} pages{method_note}

EXISTING FOLDERS (bestaande mappen; kijk of jouw folder_path hier al in voorkomt → action "existing", anders "new"):
{existing_folders}

DOCUMENT TEKST (volledig inlezen voor samenvatting; gebruik ook voor classificatie):
{text_for_call}

═══════════════════════════════════════════════════════════════════
REGELS
═══════════════════════════════════════════════════════════════════

1. BEPAAL HET TYPE DOCUMENT (uit inhoud, niet uit bestandsnaam):
   - Huurcontract / huurovereenkomst → CONTRACT, type Huurcontracten
   - EPC / energieprestatiecertificaat / energiedocument → CONTRACT, type EPC
   - Asbestattest / asbestinventaris / asbestvrijverklaring → CONTRACT, type Asbest
   - Eigendomstitel / akte → CONTRACT, type Eigendomstitel
   - Koopcontract / verkoopovereenkomst → CONTRACT, type Koopcontracten
   - Verhaal, essay, persoonlijke tekst, narratief, kort verhaal → NIET CONTRACT → folder_path = /Verhaal (NOOIT /Onbekend_adres)
   - Certificaat, verklaring, studentenverklaring, bewijs van deelname → NIET CONTRACT → folder_path = /Onderwijs (NOOIT /Onbekend_adres)
   - Onderwijs, college, cursus, studie, dictaat → NIET CONTRACT → map /Onderwijs (eventueel /Onderwijs/Subcategorie)
   - Factuur, offerte, betalingsdocument → NIET CONTRACT → map /Facturen
   - Overige zakelijke documenten → map die past bij inhoud (bv. /Correspondentie, /Rapporten)
   - Twijfel of onduidelijk → /Overig

2. CONTRACTEN (huur, EPC, asbest, eigendomstitel, koop, …):
   - folder_path = /Contracten/[Provincie]/[Adres]
   - Eén map per PAND: alle documenten van hetzelfde adres (huur, EPC, asbest, …) komen in dezelfde map.
   - Provincie = Belgische provincie op basis van adres/postcode in het document. Gebruik exact: Antwerpen, Limburg, Oost-Vlaanderen, Vlaams-Brabant, West-Vlaanderen, Brussel, Waals-Brabant, Henegouwen, Luik, Luxemburg, Namen. Geen spaties in mapnaam (bv. Oost-Vlaanderen).
   - Adres = straat + nummer uit de tekst (bv. Kerkstraat_10, Meir_78_bus_3). Geen adres in tekst → Onbekend_adres.
   - Zelfde provincie + zelfde adres: als die map al in EXISTING FOLDERS staat → action "existing", anders "new".
   - suggested_filename = Adres_Type.pdf (bv. Kerkstraat_10_huurcontract.pdf, Meir_78_bus_3_EPC.pdf, Kerkstraat_10_asbest.pdf) zodat huur en EPC van hetzelfde pand in dezelfde map staan.
   Voorbeelden: /Contracten/Antwerpen/Kerkstraat_10, /Contracten/Antwerpen/Meir_78_bus_3, /Contracten/Oost-Vlaanderen/Onbekend_adres.

3. NIET-CONTRACTEN (verhaal, certificaat, verklaring, onderwijs, factuur, …):
   - Verhaal/essay/narratief → folder_path = /Verhaal. Certificaat/verklaring/studentenverklaring → folder_path = /Onderwijs. NOOIT /Onbekend_adres.
   - Onderwijs → /Onderwijs of /Onderwijs/Sub. Factuur → /Facturen. Overig → /Teksten, /Overig, etc.
   - Geen adres in het pad. suggested_filename = korte beschrijvende naam (letters, cijfers, underscores), eindig op .pdf.

4. ACTION:
   - "existing" = folder_path staat al in EXISTING FOLDERS (zelfde pad gebruiken).
   - "new" = folder_path bestaat nog niet, wordt aangemaakt.

VERPLICHT: Geef ALTIJD action, folder_path, confidence (0-100), reasoning, suggested_filename, EN summary.
- folder_path altijd met leading slash, delen gescheiden door /, geen spaties in mapnamen (gebruik underscore).
- suggested_filename: alleen letters, cijfers, underscores; eindig op .pdf.
- summary: uitgebreide samenvatting van het document, ongeveer 300 woorden (korter alleen als er onvoldoende relevante inhoud is). Gebaseer op de volledige tekst; noem partijen, adres, bedragen, termijnen en andere belangrijke bepalingen.

ANTWOORD FORMAT (ALLEEN JSON, geen tekst ervoor/erna):

Voorbeeld CONTRACT (huurcontract Kerkstraat 10 Antwerpen, map bestaat al):
{{
  "action": "existing",
  "folder_path": "/Contracten/Antwerpen/Kerkstraat_10",
  "confidence": 95,
  "reasoning": "Huurcontract voor Kerkstraat 10 (Antwerpen); map /Contracten/Antwerpen/Kerkstraat_10 bestaat al.",
  "suggested_filename": "Kerkstraat_10_huurcontract.pdf",
  "summary": "Huurcontract tussen verhuurder X en huurder Y voor Kerkstraat 10. Looptijd 3 jaar, huurprijs 850 euro. Waarborg twee maanden."
}}

Voorbeeld CONTRACT (EPC Meir 78 bus 3 Antwerpen, zelfde pand als huur):
{{
  "action": "existing",
  "folder_path": "/Contracten/Antwerpen/Meir_78_bus_3",
  "confidence": 98,
  "reasoning": "EPC voor Meir 78 bus 3 (Antwerpen); map voor dit pand bestaat al (huur staat er al).",
  "suggested_filename": "Meir_78_bus_3_EPC.pdf",
  "summary": "EPC voor Meir 78 bus 3. Energielabel C. Bewoonbare oppervlakte 95 m². Geldig tot 2030."
}}

Voorbeeld CONTRACT (Asbestattest Kerkstraat 10, zelfde map als huur):
{{
  "action": "existing",
  "folder_path": "/Contracten/Antwerpen/Kerkstraat_10",
  "confidence": 95,
  "reasoning": "Asbestattest voor Kerkstraat 10; map /Contracten/Antwerpen/Kerkstraat_10 bestaat al.",
  "suggested_filename": "Kerkstraat_10_asbest.pdf",
  "summary": "Asbestattest voor Kerkstraat 10. Pand asbestvrij verklaard. Datum attest vermeld."
}}

Voorbeeld NIET-CONTRACT (verhaal) — ALTIJD /Verhaal, NOOIT /Onbekend_adres:
{{
  "action": "new",
  "folder_path": "/Verhaal",
  "confidence": 90,
  "reasoning": "Verhaal/essay/narratief, geen contract; hoort in map Verhaal. Onbekend_adres is alleen voor contracten zonder adres.",
  "suggested_filename": "verhaal_document.pdf",
  "summary": "Persoonlijk verhaal over een reis. Geen contract of officieel document."
}}
FOUT: verhaal of certificaat in /Onbekend_adres plaatsen. Onbekend_adres = ALLEEN voor contracten (huur, EPC, …) waar het adres ontbreekt.

Voorbeeld NIET-CONTRACT (certificaat/verklaring) — ALTIJD /Onderwijs, NOOIT /Onbekend_adres:
{{
  "action": "existing",
  "folder_path": "/Onderwijs",
  "confidence": 95,
  "reasoning": "Certificaat van deelname / studentenverklaring; geen contract. Hoort in /Onderwijs.",
  "suggested_filename": "certificaat_verklaring.pdf",
  "summary": "Certificaat van deelname aan een cursus. Geen contract."
}}

Voorbeeld NIET-CONTRACT (onderwijs):
{{
  "action": "existing",
  "folder_path": "/Onderwijs/Bedrijfskunde",
  "confidence": 92,
  "reasoning": "Onderwijsmateriaal bedrijfskunde; map bestaat al.",
  "suggested_filename": "college_week3.pdf",
  "summary": "College-notities bedrijfskunde week 3. Geen contract."
}}

JSON:"""


# =============================================================================
# EXTRACTIE HUURCONTRACT (multi-stage)
# =============================================================================

SOURCE_QUOTE_INSTRUCTION = """
BRON PER VELD: Voor elk veld dat je invult: geef indien mogelijk een object met "value" en "source_quote" in plaats van alleen een string. "source_quote" = de EXACTE zin of frase uit het contract, letterlijk zoals het er staat (inclusief spaties, punten, komma's). Voorbeeld: "naam": {{ "value": "Jan Janssen", "source_quote": "De verhuurder is Jan Janssen." }}. Voor BEDRAGEN EN GETALLEN: behoud de notatie uit het document in "source_quote" (bv. "2.300", "€ 1.150,00") zodat we kunnen controleren; "value" mag je normaliseren. Als je de bron niet kunt aanwijzen, geef dan alleen de string (zoals nu).
"""

# When numbered text is included in the prompt, ask AI to also return word_ids for exact PDF highlight.
WORD_IDS_INSTRUCTION = """
WOORD-IDS: In de genummerde tekst hieronder heeft elk woord een [getal]. Voor elk veld dat je invult met een object (value + source_quote), voeg ook "word_ids" toe: een array van die getallen die exact dat woord/de woorden in de tekst aanwijzen. Voorbeeld: als "3000" overeenkomt met [45], geef "word_ids": [45]. Meerdere woorden: "word_ids": [44, 45, 46]. Alleen de IDs van de woorden die de waarde vormen; geen IDs van omliggende tekst.
"""

PROMPT_PARTIJEN = """Je bent een expert in Belgische huurcontracten. 

TAAK: Extraheer ALLE informatie over verhuurder(s) en huurder(s).

ZOEK SPECIFIEK NAAR:
- Volledige namen (voor + achternaam, of bedrijfsnaam)
- Adressen (straat + nummer + bus + postcode + stad)
- Telefoonnummers (vast + GSM)
- Email adressen
- BTW nummers (voor bedrijven)
- Rijksregisternummers

BELANGRIJK:
- Als er MEERDERE huurders zijn → combineer namen met " & "
- Als adres NIET vermeld → gebruik "ONTBREKEND"
- Als telefoon NIET vermeld → gebruik "ONTBREKEND"
- Kopieer exacte spelling uit contract

CONTRACT TEKST:
{text_chunk_1}

VOORBEELD OUTPUT (volg deze structuur EXACT):
{{
  "verhuurder": {{
    "naam": "Vastgoed Beheer NV",
    "adres": "Industrielaan 5, 9000 Gent",
    "telefoon": "+32 9 123 45 67",
    "email": "info@vastgoedbeheer.be"
  }},
  "huurder": {{
    "naam": "Marie Dupont & Peter Vermeulen",
    "adres": "Voorlopig adres: Kerkstraat 5, 1000 Brussel",
    "telefoon": "+32 2 987 65 43",
    "email": "marie.dupont@email.be"
  }}
}}
{source_quote_instruction}
ALLEEN JSON (geen tekst ervoor/erna):"""

PROMPT_PAND = """Je bent een expert in Belgische huurcontracten en vastgoeddocumenten.

TAAK: Extraheer ALLE informatie over het gehuurde pand. Deze data wordt gebruikt voor zoeken en overzicht (EPC, kadastraal, asbest, pandoverzicht). Wees zo volledig en precies mogelijk.

══════════════════════════════════════════════════════════════════════════════
1. ADRES EN WOONKENMERKEN
══════════════════════════════════════════════════════════════════════════════
- Volledig adres: straat, nummer, bus, postcode, stad (exact zoals in document)
- Type woning: appartement, huis, studio, duplex, enz.
- Bewoonbare oppervlakte in m² (alleen getal, geen "m²")
- Aantal slaapkamers (JSON: aantal_kamers): zoek expliciet naar "slaapkamers" in het document (bv. in samenstelling: "4 slaapkamers, 2 badkamers"). Geef het getal dat bij slaapkamers hoort (bv. 4). Als niet vermeld → "ONTBREKEND" of null. Negeer andere getallen (badkamers, toiletten, artikels, datums).
- Verdieping: getal dat de verdieping(situatie) van het gehuurde pand weergeeft.
  * Als het contract één verdieping noemt (bv. "gelegen op de 2e verdieping", "gelijkvloers"): geef dat getal (0 = gelijkvloers/kelder, 1 = 1e, 2 = 2e, …).
  * Als het contract de woning beschrijft als meerdere niveaus/delen (bv. "gelijkvloers + 1e verdiep + zolder", "gelijkvloers en kelder", "benedenverdiep, eerste verdieping en zolder"): tel die genoemde niveaus en geef dat aantal (bv. 3 of 2). Geen vaste lijst hardcoden — herken zulke opsommingen en tel ze.
  * Negeer andere getallen (adres, artikels).
- Eventueel: bouwjaar, staat van het pand

══════════════════════════════════════════════════════════════════════════════
2. EPC (Energieprestatiecertificaat) — VOLLEDIG UITLEZEN
══════════════════════════════════════════════════════════════════════════════
- energielabel: exacte letter/klasse (A++, A+, A, B, C, D, E, F, G)
- certificaatnummer: volledig nummer (bv. 20231205-0001234-00000001 of formaat op het attest)
- geldig_tot: einddatum geldigheid EPC (formaat YYYY-MM-DD); als alleen jaar → YYYY-12-31
- bewoonbare_oppervlakte_epc: oppervlakte in m² zoals op EPC vermeld (getal)
- primair_energieverbruik: indien vermeld (kWh/m² jaar of totaal)
- referentiejaar: indien vermeld op EPC
Als EPC niet in de tekst staat → gebruik "ONTBREKEND" per veld.

══════════════════════════════════════════════════════════════════════════════
3. KADASTER — VOLLEDIG UITLEZEN
══════════════════════════════════════════════════════════════════════════════
- afdeling: kadastrale afdeling (bv. "Brussel 1e afdeling", "Antwerpen 2e afdeling")
- sectie: kadastrale sectie (letter of code)
- nummer: perceelnummer / lotnummer (bv. "123/4B", "456", "789/C")
- kadastraal_inkomen: bedrag in euro (alleen getal)
- gemeente_kadaster: gemeente volgens kadaster indien anders dan adres
- grondnummer: indien vermeld
Kadastraal inkomen = KI; zoek ook naar "kadastraal inkomen", "KI", "indexcijfer". Als niet vermeld → "ONTBREKEND" of null.

══════════════════════════════════════════════════════════════════════════════
4. ASBEST (Asbestattest / asbestinventaris) — VOLLEDIG UITLEZEN
══════════════════════════════════════════════════════════════════════════════
- status: "asbestvrij" / "bevat_geen_asbest" / "bevat_asbest" / "niet_onderzocht" / "ONTBREKEND"
- datum_attest: datum van het asbestattest (YYYY-MM-DD)
- referentienummer: referentie- of attestnummer indien vermeld
- opmerking: korte opmerking indien van toepassing (bv. "asbestvrij verklaard na verwijdering")
- geldig_tot: indien geldigheidsduur vermeld
Zoek naar: asbestattest, asbestinventaris, asbestvrij, asbestvrijverklaring, bevat geen asbest, asbest bevat. Als geen asbestinfo in document → status "ONTBREKEND", rest leeg of null.

══════════════════════════════════════════════════════════════════════════════
5. OVERZICHT
══════════════════════════════════════════════════════════════════════════════
Deze velden samen vormen het "overzicht" per pand. Vul elk veld in dat je in de tekst vindt; gebruik "ONTBREKEND" of null alleen als het echt ontbreekt. Geen veld weglaten in de JSON-structuur.

BELANGRIJK (altijd toepassen):
- Oppervlakte = alleen het getal (geen "m²", geen eenheid). Ook bewoonbare_oppervlakte_epc = getal.
- Kadastraal inkomen = bedrag in euro, alleen het getal (geen €, geen eenheid).
- aantal_kamers = aantal slaapkamers: zoek naar "slaapkamers" in de tekst en geef dat getal. Verdieping: bij opsomming van niveaus het aantal als getal. Geen losse getallen uit artikels of adressen.
- Als iets NIET in de tekst vermeld staat → gebruik "ONTBREKEND" (string) of null voor optionele velden.
- Kopieer exacte adressen zoals in het contract (geen afkortingen tenzij zo in document).

CONTRACT TEKST:
{text_chunk_1}

Extra context (voor EPC/kadaster/asbest die later in document staan):
{text_chunk_2}

VOORBEELD OUTPUT (volg deze structuur; alle sleutels aanwezig, ontbrekende waarden "ONTBREKEND" of null):
{{
  "adres": "Kerkstraat 10 bus 3, 1000 Brussel",
  "type": "appartement",
  "oppervlakte": 85.5,
  "aantal_kamers": 3,
  "verdieping": 2,
  "epc": {{
    "energielabel": "B",
    "certificaatnummer": "20231205-0001234-00000001",
    "geldig_tot": "2030-12-31",
    "bewoonbare_oppervlakte_epc": 85,
    "primair_energieverbruik": null,
    "referentiejaar": null
  }},
  "kadaster": {{
    "afdeling": "Brussel 1e afdeling",
    "sectie": "A",
    "nummer": "123/4B",
    "kadastraal_inkomen": 1234.56,
    "gemeente_kadaster": null,
    "grondnummer": null
  }},
  "asbest": {{
    "status": "asbestvrij",
    "datum_attest": "2024-06-15",
    "referentienummer": "ATT-2024-12345",
    "opmerking": null,
    "geldig_tot": null
  }}
}}
{source_quote_instruction}
ALLEEN JSON (geen tekst ervoor of erna):"""

PROMPT_FINANCIEEL = """Je bent een expert in Belgische huurcontracten.

TAAK: Extraheer ALLE financiële informatie.

ZOEK SPECIFIEK NAAR:
- Maandelijkse huurprijs (bedrag in euro)
- Waarborg/huurwaarborg bedrag
- Bank waar waarborg gedeponeerd is (naam + IBAN rekening)
- Gemeenschappelijke kosten (wat is inbegrepen)
- Privélasten (energie, water, gas, internet)
- Indexatie (ja/nee) en HOE dit geformuleerd is (bv. “jaarlijks op basis van de gezondheidsindex”)

BELANGRIJK:
- Huurprijs = alleen het getal (geen € teken)
- Waarborg bedrag = alleen het getal
- waar_gedeponeerd = "Banknaam — rekening BE12 3456 7890 1234" of "Banknaam (rekening BE12 3456 7890 1234)". BEHOUD ALLE leestekens zoals streepjes/dash (—, -), spaties en notatie EXACT zoals in het contract.
- waar_gedeponeerd MOET een object zijn met value + source_quote (+ word_ids indien genummerde tekst aanwezig is). source_quote = de exacte regel uit het contract met banknaam + IBAN; word_ids = ALLE woord-IDs van die regel, zodat de juiste regel gefluoriseerd kan worden in de PDF.
- kosten = beschrijving in tekst (wat inbegrepen, wat apart)
- indexatie = true/false. Mapping:
  * Als er staat dat de huur jaarlijks/geperiodiseerd wordt geïndexeerd (bv. “jaarlijkse indexering”, “jaarlijks op basis van de gezondheidsindex”, “wordt geïndexeerd volgens de wet”) → indexatie = true.
  * Als er expliciet staat dat er GEEN indexatie is (bv. “niet geïndexeerd”, “geen indexatie”, “indexering niet van toepassing”) → indexatie = false.
  * Als er niets over indexatie staat → indexatie = false.

CONTRACT TEKST:
{text_chunk_1}

Extra context:
{text_chunk_2}

VOORBEELD OUTPUT:
{{
  "huurprijs": 1150.0,
  "waarborg": {{
    "bedrag": 3450.0,
    "waar_gedeponeerd": {{
      "value": "Triodos Bank — BE67 5230 8877 6655",
      "source_quote": "Triodos Bank — BE67 5230 8877 6655",
      "word_ids": [201, 202, 203, 204, 205, 206]
    }}
  }},
  "kosten": "Gemeenschappelijke kosten (verwarming, water, lift) zijn inbegrepen in de huurprijs. Privélasten (elektriciteit, gas, internet) zijn voor rekening van huurder.",
  "indexatie": true,
  "gemeenschappelijke_kosten": {{
    "inbegrepen": [
      {{"post": "Verwarming"}},
      {{"post": "Water"}},
      {{"post": "Lift"}},
      {{"post": "Gemeenschappelijke delen"}}
    ]
  }}
}}
{source_quote_instruction}
ALLEEN JSON:"""

PROMPT_PERIODES = """Je bent een expert in Belgische huurcontracten.

TAAK: Extraheer ALLE informatie over periodes en termijnen.

ZOEK SPECIFIEK NAAR:
- Ingangsdatum / aanvangsdatum (datum wanneer huur start)
- Einddatum (als contract bepaalde duur heeft)
- Duur van het contract (bijv. "9 jaar", "3 jaar", "onbepaalde duur")
- Opzegtermijn voor huurder (hoeveel maanden)
- Opzegtermijn voor verhuurder (hoeveel maanden)
- Eventuele verlengingsvoorwaarden

BELANGRIJK:
- Datums in formaat YYYY-MM-DD (bijv. "2025-05-01")
- Als geen einddatum → "ONTBREKEND"
- Opzegtermijnen apart voor huurder en verhuurder
- Duur = letterlijk zoals in contract staat

CONTRACT TEKST:
{text_chunk_1}

Extra context:
{text_chunk_2}

VOORBEELD OUTPUT:
{{
  "ingangsdatum": "2025-05-01",
  "einddatum": "ONTBREKEND",
  "duur": "9 jaar",
  "opzegtermijn_huurder": "3 maanden",
  "opzegtermijn_verhuurder": "6 maanden"
}}
{source_quote_instruction}
ALLEEN JSON:"""

PROMPT_VOORWAARDEN = """Je bent een expert in Belgische huurcontracten.

TAAK: Extraheer ALLE bijzondere voorwaarden en bepalingen.

ZOEK SPECIFIEK NAAR:
- Huisdieren: WAT staat er precies? (niet alleen ja/nee — bv. "honden niet, katten niet, vissen wel", "alleen kleine kooidieren", "met toestemming verhuurder")
- Onderverhuur toegestaan? (ja/nee)
- Werken/verbouwingen (wat mag/niet mag)
- Andere bijzondere bepalingen

BELANGRIJK:
- huisdieren_toelating = de VOLLEDIGE, LETTERLIJKE tekst uit het contract over huisdieren. Bijv. "Honden en katten zijn niet toegestaan. Vissen en kleine kooidieren zijn wel toegestaan." Of "Huisdieren zijn verboden." Geen samenvatting zoals alleen "ja" of "nee" — geef de exacte bewoording zodat duidelijk is wat wel/niet mag. Als er niets over huisdieren staat: "ONTBREKEND".
- onderverhuur = true/false
- werken = beschrijving in tekst
- BRON EN MARKERING: Voor huisdieren_toelating MOET je source_quote en word_ids geven. source_quote = de exacte zin(nen) uit het contract over huisdieren (letterlijk overnemen). word_ids = de IDs van ALLE woorden van die passage in de genummerde tekst, zodat in de PDF precies die zin wordt gemarkeerd (gefluoriseerd). Wijs ALLEEN de zin over huisdieren aan, niet een andere zin met "toegestaan" of "verboden".

CONTRACT TEKST:
{text_chunk_1}

Extra context:
{text_chunk_2}

VOORBEELD OUTPUT:
{{
  "huisdieren_toelating": {{ "value": "Honden en katten zijn niet toegestaan. Vissen en kleine kooidieren zijn wel toegestaan.", "source_quote": "Honden en katten zijn niet toegestaan. Vissen en kleine kooidieren zijn wel toegestaan.", "word_ids": [123, 124, 125, 126, 127, 128, 129, 130, 131, 132, 133, 134, 135] }},
  "onderverhuur": false,
  "werken": "Kleine herstellingswerken toegestaan. Grotere verbouwingen enkel met schriftelijke toestemming van verhuurder."
}}
{source_quote_instruction}
ALLEEN JSON:"""

PROMPT_JURIDISCH = """Je bent een expert in Belgische huurcontracten.

TAAK: Extraheer juridische bepalingen.

ZOEK SPECIFIEK NAAR:
- Toepasselijk recht (bijv. "Vlaams Woninghuurdecreet")
- Bevoegde rechtbank / vrederechter (bijv. "Vrederechter van het kanton Gent")

CONTRACT TEKST:
{text_chunk_1}

Extra context:
{text_chunk_2}

VOORBEELD OUTPUT:
{{
  "toepasselijk_recht": "Vlaams Woninghuurdecreet van 9 november 2018",
  "bevoegde_rechtbank": "Vrederechter van het kanton Brussel"
}}
{source_quote_instruction}
ALLEEN JSON:"""

PROMPT_METADATA = """Je bent een expert in Belgische huurcontracten.

TAAK: Extraheer algemene contract informatie.

ZOEK SPECIFIEK NAAR:
- Type contract (huurovereenkomst/huurcontract)
- Datum van ondertekening contract

CONTRACT TEKST:
{text_chunk_1}

VOORBEELD OUTPUT:
{{
  "contract_type": "huurovereenkomst",
  "datum_contract": "2025-03-18"
}}
{source_quote_instruction}
ALLEEN JSON:"""


# =============================================================================
# EPC-DOCUMENT (EPB-certificaat / energieprestatiecertificaat)
# =============================================================================
# Placeholder: text_chunk_1 (volledige tekst), eventueel text_chunk_2 voor extra context

PROMPT_EPC = """Je bent een vastgoeddata-analist gespecialiseerd in Belgische EPB-certificaten (EPC).
Analyseer het meegestuurde EPB-certificaat en geef ALLEEN een geldig JSON-object terug, zonder uitleg of markdown.

REGELS (strikt volgen):
- Gebruik ALLEEN waarden die expliciet in het document staan of die je exact kunt berekenen (bv. jaarlijkse kost uit verbruik × prijs). Bij twijfel of ontbreken: null.
- Datums altijd in formaat YYYY-MM-DD. Getallen als getal (geen strings), behalve adres en vrije tekst.
- Adres en energieklasse: letterlijk overnemen zoals op het certificaat (geen afkortingen tenzij zo vermeld).
- Geen velden invullen met gokken of aannames. Liever null dan fout.

Extraheer én BEREKEN (alleen indien in document of exact berekenbaar) de volgende velden:

IDENTIFICATIE:
- adres: volledig adres van het gebouw/eenheid (zoals op certificaat)
- appartement_nr: appartementsnummer indien van toepassing
- vloeroppervlakte_m2: bewoonbare oppervlakte in m² (getal)
- volume_m3: volume indien vermeld (getal)
- geldig_tot: einddatum geldigheid (YYYY-MM-DD)
- afgeleverd_op: datum aflevering attest (YYYY-MM-DD)

ENERGIEPRESTATIE:
- energieklasse: A++, A+, A, B+, B, B-, C+, C, C-, D t/m G
- epb_score: numerieke EPB-score indien vermeld
- co2_uitstoot_kg_m2_jaar: CO₂-uitstoot in kg/m²/jaar
- netto_energiebehoefte_verwarming: kWh/m²
- primair_verbruik_per_m2: primair energieverbruik per m²
- totaal_verbruik_jaar_kwh: totaal verbruik per jaar in kWh

GESCHATTE ENERGIEKOST (bereken indien mogelijk):
- geschatte_jaarlijkse_energiekost_eur: totaal kWh * ca. 0,30 EUR (gem. Belgische prijs)
- geschatte_maandelijkse_energiekost_eur: jaarlijkse kost / 12, afgerond op 5 EUR
- energiekost_label: "Zeer laag (<50€/mnd)" | "Laag (50-100€)" | "Gemiddeld (100-150€)" | "Hoog (150-250€)" | "Zeer hoog (>250€/mnd)"

INSTALLATIES:
- verwarmingssysteem: bv. "Warmtepomp", "Condenserende ketel"
- verwarming_collectief: true/false
- sanitair_warm_water: bv. "Condenserende ketel"
- sww_collectief: true/false
- ventilatie_type: A, B, C of D
- hernieuwbare_energie: true indien zonnepanelen/warmtepomp/hernieuwbaar

ISOLATIE & COMFORT:
- u_waarde_venster: W/m².K (lager = beter)
- u_waarde_opaque: W/m².K
- luchtdichtheid_m3_h_m2: indien vermeld
- oververhitting_risico: "Laag" | "Gemiddeld" | "Hoog"
- ventilatie_conform: true/false

EPB-CONFORMITEIT:
- voldoet_nev: true indien netto energiebehoefte ≤ 15 kWh/m²
- voldoet_primair_verbruik: true/false
- voldoet_isolatie: true/false
- aantal_niet_conform: aantal EPB-eisen die niet gehaald zijn (getal)

SCORES (schaal 1-10, zelf inschatten op basis van certificaat):
- score_energiezuinigheid: 10 = A++, 1 = G
- score_comfort: o.b.v. ventilatie, oververhitting, luchtdichtheid
- score_toekomstbestendigheid: hoog bij warmtepomp + hernieuwbaar + goede isolatie
- score_totaal: gewogen gemiddelde (40% energiezuinig + 30% comfort + 30% toekomst)

MATCHING:
- profiel_tags: array van tags, kies uit: "Zeer energiezuinig", "Lage energiekost", "Warmtepomp", "Zonnepanelen", "Collectief systeem", "Instapklaar", "Renovatienodig", "Ideaal voor huurder", "Ideaal voor investeerder", "Toekomstbestendig", "Nieuwbouw kwaliteit"

SAMENVATTING:
- verkoop_argument_energie: max 2 zinnen voor makelaar (bv. "Energieklasse B+, geschatte energiekost €52/maand dankzij warmtepomp.")
- aandachtspunten: array van zwakke punten (bv. "Ventilatie type C: minder comfortabel dan D")

DOCUMENT TEKST (EPB-certificaat):
{text_chunk_1}

Extra context (indien nodig):
{text_chunk_2}

Geef ALLEEN het JSON-object. Alle sleutels aanwezig; gebruik null waar het veld ontbreekt of onzeker is. Geen tekst voor of na de JSON."""


# =============================================================================
# SAMENVATTING (na extractie, voor website)
# =============================================================================

PROMPT_SUMMARY = """Maak een uitgebreide samenvatting van dit {doc_type} van ongeveer 300 woorden (korter alleen als er niet genoeg relevante informatie in de tekst staat).

Focus op:
- Partijen (verhuurder en huurder)
- Pand (adres, type, oppervlakte, EPC/asbest indien vermeld)
- Financiële voorwaarden (huurprijs, waarborg, indexatie)
- Termijnen (ingangsdatum, duur, opzegtermijnen)
- Bijzondere bepalingen en andere inhoudelijk belangrijke punten

CONTRACT TEKST:
{text_sample}

Geef ALLEEN de samenvatting (geen introductie). Streef naar circa 300 woorden."""


# =============================================================================
# E-MAIL (na verwerking contract)
# =============================================================================
# Placeholders: filename, title, details_section, summary
# EMAIL_IMAGE_PATH: pad naar een PNG die als bijlage meegaat (bijv. "logo.png" in parser-map), of None

# Relatief t.o.v. parser-map: ../public/ = projectroot/public/
EMAIL_IMAGE_PATH = "../public/DataFuse-logo-name.png"

EMAIL_SUBJECT = "Contract verwerkt: {title} – {filename}"

EMAIL_BODY = """Beste,

Hieronder de verwerkte gegevens van het contract.

DOCUMENT
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Bestand: {filename}
Type: {title}

KERNGEGEVENS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━{details_section}
SAMENVATTING
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{summary}

Met vriendelijke groet,
DataFuse"""
