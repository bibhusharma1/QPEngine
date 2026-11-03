# Reconstructor

The Reconstructor reads a parsed question bank and writes an editable DOCX. It preserves question order, labels, sections, marks, source metadata, and linked figures or tables where available. Each document ends with one accuracy notice and a source-reference list.

Use `POST /subjects/{subject}/pdfs/{filename}/reconstruct` after processing. Output goes to `<subject>/output/Reconstruct/<PDF stem>/questions_reconstructed.docx`; it does not overwrite Scrapper or Parser files.

Missing assets and DOCX errors are returned to the caller. A missing figure is not recreated or guessed.

