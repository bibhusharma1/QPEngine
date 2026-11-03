# Structured-output test fixtures for the LLM-assisted nodes.
#
# Kept as plain functions (not JSON files) so tests can build variants
# with meaningful names instead of loading blobs from disk.

QUESTION_SEGMENT_MOCK_TEXT = "\n".join([
    "BIRLA INSTITUTE OF TECHNOLOGY, MESRA, RANCHI",
    "(MID SEMESTER EXAMINATION)",
    "CLASS: BTECH                      SEMESTER : III",
    "BRANCH: AI & ML                   SESSION : MO/2025",
    "      SUBJECT: AI24201 MATHEMATICS FOR AI & ML",
    "TIME: 02 HOURS                    FULL MARKS: 25",
    "INSTRUCTIONS:",
    "1. The question paper contains 5 questions each of 5 marks and total 25 marks.",
    "2. Attempt all questions.",
    "3. The missing data, if any, may be assumed suitably.",
    "----------------------------------------------------------------",
    "",
    "CO  BL",
    "Q.1(a)  Assume a situation where 5 friends Aarav, Meera, Neha, Ravi, Tara were  [0.5+1+0.5   1    4",
    "        sending messages to each other                                          +1+1+1]",
    "        (i)   Identify set and relations in this situation.",
    "        (ii)  Construct a graph of it.",
    "",
    "Q.2(a)  Answer the part (a) and (b) of this question based on the figure given below:   [1]         3    3",
    "",
    "Q.2(b)  Identify probability distribution shown in above figure? Justify your answer? [1]         3    3",
    "",
    "Q.3    Find the eigen values of the matrix given below",
    "           8  −6   2",
    "       A=  −6   7  −4",
    "           2  −4   3",
])

SUB_SEGMENTS_TEMPLATE = {
    "exam_title": "MID SEMESTER EXAMINATION",
    "institution": "BIRLA INSTITUTE OF TECHNOLOGY, MESRA, RANCHI",
    "course_code": "AI24201",
    "subject": "MATHEMATICS FOR AI & ML",
    "session": "MO/2025",
    "semester": "III",
    "full_marks": 25,
    "num_main_questions": 3,
    "questions": [
        {
            "label": "Q.1",
            "text": "Assume a situation where 5 friends send messages to each other.",
            "marks": "0.5+1+0.5+1+1+1",
        },
        {
            "label": "Q.2",
            "text": "Answer the parts (a) and (b) based on the figure given below.",
            "marks": "1+1",
        },
        {
            "label": "Q.3",
            "text": "Find the eigen values of the matrix given below.",
            "marks": "3",
        },
    ],
}

CROPPED_SEGMENT_TEMPLATE = {   # truncated evidence for invalid-JSON tests
    "full_marks": 25,
    "questions": [
        {"label": "Q.1", "text": "truncated…"},
    ],
}
