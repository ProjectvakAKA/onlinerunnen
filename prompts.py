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
   - folder_path = /Contracten/[TypeMap]/[Adres]
   - TypeMap = Huurcontracten, EPC, Asbest, Eigendomstitel, Koopcontracten.
   - Adres = straat + nummer uit de tekst (bv. Kerkstraat_10, Meir_78_bus_3). Alleen bij CONTRACTEN: als geen adres in tekst → Onbekend_adres.
   - Zelfde adres + zelfde type: als die map al in EXISTING FOLDERS staat → action "existing", anders "new".
   - suggested_filename = Adres_Type.pdf (bv. Kerkstraat_10_huurcontract.pdf, Meir_78_bus_3_EPC.pdf, Kerkstraat_10_asbest.pdf).
   Voorbeelden: /Contracten/Huurcontracten/Kerkstraat_10, /Contracten/EPC/Meir_78_bus_3, /Contracten/Asbest/Kerkstraat_10, /Contracten/Eigendomstitel/Onbekend_adres.

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

Voorbeeld CONTRACT (huurcontract Kerkstraat 10, map bestaat al):
{{
  "action": "existing",
  "folder_path": "/Contracten/Huurcontracten/Kerkstraat_10",
  "confidence": 95,
  "reasoning": "Huurcontract voor Kerkstraat 10; map /Contracten/Huurcontracten/Kerkstraat_10 bestaat al.",
  "suggested_filename": "Kerkstraat_10_huurcontract.pdf",
  "summary": "Huurcontract tussen verhuurder X en huurder Y voor Kerkstraat 10. Looptijd 3 jaar, huurprijs 850 euro. Waarborg twee maanden."
}}

Voorbeeld CONTRACT (EPC Meir 78 bus 3, nieuwe map):
{{
  "action": "new",
  "folder_path": "/Contracten/EPC/Meir_78_bus_3",
  "confidence": 98,
  "reasoning": "Energieprestatiecertificaat voor Meir 78 bus 3; map bestaat nog niet.",
  "suggested_filename": "Meir_78_bus_3_EPC.pdf",
  "summary": "EPC voor Meir 78 bus 3. Energielabel C. Bewoonbare oppervlakte 95 m². Geldig tot 2030."
}}

Voorbeeld CONTRACT (Asbestattest Kerkstraat 10):
{{
  "action": "new",
  "folder_path": "/Contracten/Asbest/Kerkstraat_10",
  "confidence": 95,
  "reasoning": "Asbestattest / asbestvrijverklaring voor Kerkstraat 10.",
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
- Aantal kamers / slaapkamers
- Verdieping (getal of "gelijkvloers", "kelder")
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
- Indexatie (ja/nee)

BELANGRIJK:
- Huurprijs = alleen het getal (geen € teken)
- Waarborg bedrag = alleen het getal
- waar_gedeponeerd = "Banknaam (rekening BE12 3456 7890 1234)"
- kosten = beschrijving in tekst (wat inbegrepen, wat apart)
- indexatie = true/false

CONTRACT TEKST:
{text_chunk_1}

Extra context:
{text_chunk_2}

VOORBEELD OUTPUT:
{{
  "huurprijs": 1150.0,
  "waarborg": {{
    "bedrag": 3450.0,
    "waar_gedeponeerd": "Belfius Bank (rekening BE71 0961 2345 6769)"
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
- Huisdieren toegestaan? (ja/nee/met toestemming)
- Onderverhuur toegestaan? (ja/nee)
- Werken/verbouwingen (wat mag/niet mag)
- Andere bijzondere bepalingen

BELANGRIJK:
- huisdieren = true/false/"ONTBREKEND"
- onderverhuur = true/false
- werken = beschrijving in tekst

CONTRACT TEKST:
{text_chunk_1}

Extra context:
{text_chunk_2}

VOORBEELD OUTPUT:
{{
  "huisdieren": true,
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
