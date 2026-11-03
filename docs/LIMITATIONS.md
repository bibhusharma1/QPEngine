# Accuracy and runtime limits

OCR and parsing have a working target of approximately 85–90%; this is not a measured or guaranteed accuracy. OCR errors, question-boundary mistakes, missing metadata, and layout issues may need manual review against the original PDF.

Native PDF text extraction runs on CPU. Scanned pages with no native text depend on the optional OCR runtime and compatible NVIDIA hardware; without it, that job fails clearly. Short native text is retained for review when GPU OCR is unavailable. Image extraction can miss faint, overlapping, or unusual figures. Missing images are reported; the system does not invent replacements.
