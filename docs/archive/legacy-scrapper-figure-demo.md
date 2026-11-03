> Archived sample documentation from the former sampleData layout. Its remote image references are historical and do not describe current output paths.

# ques.md — working notes

Kept on purpose: where the sample PDFs came from. Full docs: `README.md`, `DOCS.md`.

## PDF sources

Portal: <https://bitmesra.ac.in/Other-Department-Pages/content/1/258/446>

- <https://bitmesra.ac.in/UploadedDocuments/adminexam/files/MO2025%20QP/AI363%20INTRODUCTION%20TO%20MACHINE%20LEARNING_MID.pdf>
- <https://bitmesra.ac.in/UploadedDocuments/adminexam/files/MO2025%20QP/CS231%20DATA%20STRUCTURES_MID.pdf>
- <https://bitmesra.ac.in/UploadedDocuments/adminexam/files/MO2025%20QP/AI24201%20MATHEMATICS%20FOR%20AIML_MID.pdf>
- <https://bitmesra.ac.in/UploadedDocuments/adminexam/files/MO2025%20QP/CS237%20DATABASE%20MANAGEMENT%20SYSTEM_MID.pdf>

Same portal students upload from — so the tool later works for real uploads too. Word/PPT ignored for now.

## Output plan

- Keep: `questions_data.json` (one record per page, `raw_text` + `figure_paths`).
- Regenerate anytime by re-running the pipeline: text in `output/<pdf_stem>/text/`, pictures in `images/`, drawn figures in `images_vector/`, report in `manifest.json`.
- Later the OCR phase uses the page images to check it can parse questions from plain images.

## Extracted figures (samples)

Rendered directly from the repo (`raw.githubusercontent.com` links, spaces URL-encoded):

![AI363 — vector drawing figure](https://raw.githubusercontent.com/bibhusharma1/QuesScrapper/main/sampleData/ques_bank/output/AI363%20INTRODUCTION%20TO%20MACHINE%20LEARNING_MID/images_vector/page_0001_drawing_1.png)

![AI24201 p1 — normal distribution figure](https://raw.githubusercontent.com/bibhusharma1/QuesScrapper/main/sampleData/ques_bank/output/AI24201%20MATHEMATICS%20FOR%20AIML_MID/images/page_0001_image_1.jpeg)

![AI24201 p2 — Markov model figure](https://raw.githubusercontent.com/bibhusharma1/QuesScrapper/main/sampleData/ques_bank/output/AI24201%20MATHEMATICS%20FOR%20AIML_MID/images/page_0002_image_1.jpeg)

Local paths (same files):

- `sampleData/ques_bank/output/AI363 INTRODUCTION TO MACHINE LEARNING_MID/images_vector/page_0001_drawing_1.png`
- `sampleData/ques_bank/output/AI24201 MATHEMATICS FOR AIML_MID/images/page_0001_image_1.jpeg`
- `sampleData/ques_bank/output/AI24201 MATHEMATICS FOR AIML_MID/images/page_0002_image_1.jpeg`
