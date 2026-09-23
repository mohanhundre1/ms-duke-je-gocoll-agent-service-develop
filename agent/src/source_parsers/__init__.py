"""Source file parsers for GOCOLL JE.

Engine 1 (Extraction): parses lockbox batch PDFs into
``GoCollExtraction`` units. The text tier (:mod:`gocoll_pdf_parser`)
reads Transaction Summary fields from the PDF text layer; the vision
tier (:mod:`vision_extractor`) transcribes GO-Collection-Form code
blocks from form images. :mod:`gocoll_coordinator` ties both tiers
together. PDF-to-image rendering is sourced from the shared
``ms_duke_je_common.vision.pdf_to_images`` module; ``llm_response_validation``
remains local reusable plumbing.
""""""Placeholder for package."""
