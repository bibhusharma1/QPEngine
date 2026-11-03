# MasterDeOrca --- Simple Project Field Report

*A simple guide to what the application does, how its parts work, and
where its information goes.*

## 1. What does the application do?

MasterDeOrca helps a user turn question-paper documents into an
organised Question Bank and then use those questions to prepare new
question papers.

In simple words:

1.  A user opens the application.
2.  The user chooses a subject and uploads a question-paper file.
3.  The backend reads the document and finds its questions.
4.  The questions are cleaned, arranged and saved in the Question Bank.
5.  The user can review questions, set priorities and prepare a new
    paper.
6.  The application can create the final paper as a DOCX file.

The application has two main sides:

-   **Frontend:** the screens, buttons, forms and tables that the user
    works with.
-   **Backend:** the part that receives requests, checks permissions,
    processes documents, saves information and sends results back.

The frontend and backend are connected through APIs. The frontend asks;
the backend does the work and replies.

## 2. The whole application at a glance

``` mermaid
flowchart TD
    U[User] --> UI[Frontend: pages and buttons]
    UI -->|API request| API[Backend: FastAPI]
    API --> AUTH[Login and permission checks]
    API --> SERVICES[Application services]
    SERVICES --> DB[(SQLite database)]
    SERVICES --> DOC[Document processing]
    DOC --> QB[Organised Question Bank]
    QB --> DB
    SERVICES --> GEN[Question paper creation]
    GEN --> DB
    DB --> SERVICES
    SERVICES -->|JSON or file response| API
    API --> UI
    UI --> U
```

Think of the frontend as the shop counter and the backend as the people
working in the shop. The counter takes the request, the backend does the
work, and the answer comes back to the screen.

## 3. Frontend: what the user sees

The frontend is the visible part of the application. It is responsible
for showing information and collecting user actions.

It contains screens and controls for:

-   Signing in and viewing the user account.
-   Viewing and managing subjects.
-   Uploading question-paper documents.
-   Starting document processing and checking its progress.
-   Viewing extracted questions and their marks.
-   Reviewing and managing the Question Bank.
-   Setting question priorities.
-   Creating and managing paper blueprints and drafts.
-   Generating and viewing question papers.

The frontend does not directly open the SQLite database. It sends API
requests to the backend and displays the replies.

### How the frontend gets information

``` mermaid
sequenceDiagram
    actor User
    participant UI as Frontend
    participant API as Backend API
    participant DB as SQLite

    User->>UI: Opens the Question Bank page
    UI->>API: GET request for questions
    API->>DB: Find matching questions
    DB-->>API: Return saved records
    API-->>UI: Return JSON response
    UI-->>User: Show questions in a table
```

### What happens when a user clicks a button?

For example, when the user opens a subject page:

1.  The frontend reads the selected subject.
2.  It sends a request to the correct API address.
3.  The backend checks the request and finds the data.
4.  The backend sends the data back, usually as JSON.
5.  The frontend reads the JSON and fills the page.

The same pattern is used for creating, editing, deleting and viewing
records.

## 4. Backend: the working part

The backend is the part that does the actual work behind the screens.

Its main responsibilities are:

-   **Login and permissions:** checks who is signed in and what that
    person is allowed to do.
-   **Subjects:** creates, lists, updates and removes subjects.
-   **Document uploads:** receives files and checks that they are
    allowed.
-   **Processing jobs:** starts document processing and keeps track of
    its status.
-   **Question Bank:** saves, finds, updates and removes questions.
-   **Priorities and blueprints:** stores question importance and paper
    layouts.
-   **Paper generation:** selects questions and prepares the final
    document.
-   **Recovery and checking:** checks unfinished jobs and helps keep
    saved records consistent.

The backend is built around a FastAPI application. Its routes receive
API requests and pass the work to the relevant service.

``` mermaid
flowchart TD
    R[API request arrives] --> V[Check login and permissions]
    V --> I[Check the request and its data]
    I --> S[Choose the required service]
    S -->|Question or user data| DB[(SQLite)]
    S -->|Upload or processing| P[Document processing]
    S -->|Paper request| G[Paper generation]
    DB --> OUT[Prepare result]
    P --> OUT
    G --> OUT
    OUT --> J[Send JSON or file response]
    J --> UI[Frontend displays the result]
```

## 5. Document reading: Scrapper and Parser

The document-reading work has two separate parts in the older local
pipeline: **Scrapper** and **Parser**. They have different jobs.

### 5.1 What does QuesScrapper do?

Think of Scrapper as the person who opens a book and collects its pages
and useful pieces.

Its job is to prepare information from the uploaded document so the next
stage can understand it. Depending on the document and processing mode,
this can include:

-   Opening and reading PDF pages.
-   Extracting text already present in a PDF.
-   Turning pages into images when needed.
-   Using OCR to read words from scanned pages or images.
-   Collecting page information and useful image/figure references.
-   Preparing extracted material for the Parser.

**Technologies in the legacy local Scrapper:**

-   **PyMuPDF:** opens PDFs, reads available text and works with pages.
-   **PaddleOCR / PaddlePaddle GPU:** reads text from scanned pages or
    images when OCR is needed.
-   **Python:** connects the steps and handles the extracted
    information.

Scrapper does not decide the final Question Bank structure by itself. It
prepares the material.

### 5.2 What does QuesParser do?

Think of Parser as the person who takes the collected material and sorts
it into a question paper.

Its job is to understand the extracted content and arrange it into
structured question records. It works out things such as:

-   Where a question starts and ends.
-   Which sub-questions belong to a parent question.
-   The question number and section.
-   The marks assigned to a question.
-   Instructions that apply to a section or group of questions.
-   Which figures or tables belong to a question.
-   The final structured question data.

The output is checked against a defined data structure before it is
passed to the Question Bank.

**Technologies in the legacy local Parser:**

-   **LangGraph:** organises the parsing work into connected steps.
-   **LangChain and model-provider integrations:** help connect language
    models used by the legacy parser.
-   **Pydantic:** checks that the output follows the expected data
    shape.
-   **Python:** handles cleanup, grouping and other predictable steps.

The local parser integrations can use configured providers such as
Gemini, Mistral, Groq, Cerebras or OpenAI. These are optional
integrations, not all required at the same time.

### 5.3 The important difference

  -----------------------------------------------------------------------
  Part                                Simple job
  ----------------------------------- -----------------------------------
  Scrapper                            Gets text, pages and images out of
                                      the document.

  Parser                              Understands the collected material
                                      and arranges it into questions.

  Question Bank                       Keeps the organised questions ready
                                      for use.
  -----------------------------------------------------------------------

### Legacy local Scrapper and Parser flow

``` mermaid
flowchart TD
    A[Uploaded PDF or image] --> B[QuesScrapper]
    B --> C[PyMuPDF reads PDF text and pages]
    B --> D[PaddleOCR reads scanned text when needed]
    C --> E[Collected text, pages and image details]
    D --> E
    E --> F[QuesParser]
    F --> G[LangGraph organises parsing steps]
    G --> H[Language model helps understand content]
    H --> I[Python arranges questions and sub-questions]
    I --> J[Pydantic checks the output]
    J --> K[Structured question data]
    K --> L[Question Bank]
```

## 6. Two processing versions: NVIDIA Lite and Local

The project has two parser paths. They should not be mixed up.

### 6.1 NVIDIA Lite version --- active path

The Lite version uses NVIDIA's document and vision models through
NVIDIA's API. It does not need the old local Scrapper and local OCR
pipeline for its normal processing path.

It works in two main stages:

1.  **Read and extract:** the PDF pages are converted into images and
    sent to NVIDIA Nemotron 3.5 Parse. It extracts the page content and
    layout information.
2.  **Understand and organise:** NVIDIA Nemotron 3.5 Super VL examines
    the extracted blocks and helps classify their meaning. Python then
    assembles the blocks into questions in a predictable way.

The result is checked against the expected question structure using
Pydantic before it is saved and synchronised with the Question Bank.

**Main technologies in Lite mode:**

-   **NVIDIA Nemotron 3.5 Parse:** reads document pages and extracts
    their content and layout.
-   **NVIDIA Nemotron 3.5 Super VL 120B A12B:** helps classify and
    understand extracted content blocks.
-   **Python:** joins the information and assembles question records.
-   **Pydantic:** checks that the final data has the expected structure.

Lite mode calls NVIDIA services over the network. The model work is not
performed by the user's local GPU.

``` mermaid
flowchart TD
    A[PDF uploaded] --> B[Convert PDF pages to images]
    B --> C[NVIDIA Nemotron 3.5 Parse]
    C --> D[Extracted text and page blocks]
    D --> E[Prepare and group blocks]
    E --> F[NVIDIA Nemotron 3.5 Super VL]
    F --> G[Classify blocks: question, instruction, figure, etc.]
    G --> H[Python assembles questions]
    H --> I[Pydantic checks the structure]
    I --> J[Save parsed result]
    J --> K[Sync questions into SQLite Question Bank]
```

### 6.2 Local version --- legacy path kept in the project

The Local version keeps the earlier Scrapper and Parser pipeline:

-   QuesScrapper uses PyMuPDF and, when required, PaddleOCR with local
    GPU support.
-   QuesParser uses LangGraph and the configured language-model
    integrations to organise the extracted content.
-   Python and Pydantic help prepare and validate the question data.

This path is kept as a separate legacy option in the codebase. The
current active processing path is NVIDIA Lite; the local pipeline is not
the active job path after the recent SQLite-only update.

``` mermaid
flowchart TD
    A[Document] --> B[Local QuesScrapper]
    B --> C[PyMuPDF and optional GPU OCR]
    C --> D[Extracted material]
    D --> E[Local QuesParser]
    E --> F[LangGraph and configured model provider]
    F --> G[Python question assembly]
    G --> H[Pydantic validation]
    H --> I[Question Bank service]
```

### 6.3 Lite and Local compared

  ------------------------------------------------------------------------------
  Item                           NVIDIA Lite             Local legacy
  ------------------------------ ----------------------- -----------------------
  Main reader                    NVIDIA Nemotron 3.5     PyMuPDF and optional
                                 Parse                   PaddleOCR

  Understanding/classification   NVIDIA Nemotron 3.5     LangGraph with
                                 Super VL                configured model
                                                         providers

  Scrapper stage                 Not used in the active  QuesScrapper
                                 Lite path               

  Local GPU for normal parsing   Not required            May be used for OCR

  Current role                   Active processing path  Kept as legacy code
                                                         path
  ------------------------------------------------------------------------------

## 7. SQLite: where the application keeps its information

The current persistence design uses **SQLite as the main source of
truth**.

SQLite is a small database stored in a database file. The backend uses
it to keep application records in an organised way.

The recent storage update moved the application's persistent data into
SQLite. This includes:

-   User accounts, roles and access information.
-   Subjects and related records.
-   Uploaded documents.
-   Processing jobs and their status.
-   Parsed question data and Question Bank records.
-   Priorities, blueprints, drafts and generated-paper records.
-   Figures, parser outputs, logs, manifests and generated DOCX files.

### Are uploaded PDFs stored inside SQLite?

Yes, in the current SQLite-only persistence design, uploaded documents
and other binary files are stored in SQLite as **BLOBs**. A BLOB is
simply a place in the database for storing file bytes.

The database can keep both: - Information about the file, such as its
name, type, subject and processing status. - The actual file content,
stored as binary data.

The same approach is used for figures and generated documents. The Lite
processing code may create temporary working files when a document
library needs a file path, but those temporary files are not the
permanent storage location.

``` mermaid
flowchart TD
    A[User uploads PDF] --> B[Backend checks file]
    B --> C[Read file bytes]
    C --> D[(SQLite database)]
    D --> E[File details and metadata]
    D --> F[PDF bytes stored as BLOB]
    G[Parser creates result] --> D
    H[Figures and generated DOCX] --> D
    D --> I[Backend reads saved data when needed]
    I --> J[API sends result or file to frontend]
```

### What is kept in the database?

  -----------------------------------------------------------------------
  Information                         What it is used for
  ----------------------------------- -----------------------------------
  Users and roles                     Who can sign in and what they can
                                      do

  Subjects                            Which subject the work belongs to

  Documents                           Uploaded file details and file
                                      content

  Jobs                                Processing status, progress and
                                      result information

  Question Bank records               Organised question data

  Priorities                          How important a question is

  Blueprints                          The layout and rules for a question
                                      paper

  Drafts and papers                   Saved and generated paper
                                      information

  Stored artifacts                    File content such as PDFs, figures,
                                      parser artifacts and DOCX files

  Logs and manifests                  Processing history and recovery
                                      information
  -----------------------------------------------------------------------

**A note about JSON:** JSON is a way to arrange information into named
fields. It is used for API communication and for representing structured
question data. It does not mean that the whole application is stored as
loose JSON files. In the current design, SQLite is the main persistent
store; JSON can be used as the format of data inside a database record
or API response.

## 8. How the upload and processing journey works

This is the full journey of one question-paper PDF.

``` mermaid
flowchart TD
    A[User selects a PDF] --> B[Frontend sends upload API request]
    B --> C[Backend checks file name, type and size]
    C --> D[Save file content in SQLite]
    D --> E[Create document record]
    E --> F[User starts processing]
    F --> G[Create processing job]
    G --> H{Choose configured parser path}
    H -->|NVIDIA Lite active| I[NVIDIA two-stage processing]
    H -->|Local legacy| J[Scrapper and Parser pipeline]
    I --> K[Structured questions]
    J --> K
    K --> L[Validate question data]
    L --> M[Save Question Bank records in SQLite]
    M --> N[Mark job as completed or with errors]
    N --> O[Frontend asks for status and results]
    O --> P[Backend returns JSON]
    P --> Q[Frontend displays questions]
```

### Why is there a job record?

Document processing may take time. The backend therefore creates a job
record rather than making the user wait for one long request.

The frontend can ask for the job status. The backend can reply with a
state such as queued, running, succeeded, completed with errors or
failed, depending on the job result.

## 9. APIs: how the frontend talks to the backend

An API is like a waiter carrying a request from the frontend to the
backend and bringing the answer back.

The frontend sends an HTTP request to an API route. The route performs
the required action and usually returns JSON. When the user requests an
actual file, the backend can return the file instead.

The local development API address is:

``` text
http://localhost:8000
```

The frontend uses a shared API helper, so pages do not each need to
build the network request in a different way. The API base address can
be configured through `window.QPG_API_BASE`.

### Common API actions

  ----------------------------------------------------------------------------------------------------
  What the user does      Example API route                                    What comes back
  ----------------------- ---------------------------------------------------- -----------------------
  Sign in                 `POST /auth/login`                                   Login token and token
                                                                               type

  Get current user        `GET /auth/me`                                       User details and role

  List subjects           `GET /subjects`                                      Subject list

  Upload PDFs             `POST /subjects/{subject}/pdfs`                      Upload result

  Start processing        `POST /subjects/{subject}/pdfs/{filename}/process`   Job information

  Check a job             `GET /jobs/{job_id}`                                 Job status

  Get parsed result       `GET /subjects/{subject}/pdfs/{filename}/result`     Parsed question data

  List Question Bank      `GET /api/question-bank`                             Question records and
                                                                               paging information

  Get one question        `GET /api/question-bank/{question_id}`               One question

  Update a question       `PATCH /api/question-bank/{question_id}`             Updated record or
                                                                               confirmation

  List blueprints         `GET /blueprints`                                    Blueprint list

  Generate a paper        `POST /api/question-papers/generate`                 Generated-paper details
  ----------------------------------------------------------------------------------------------------

The `{subject}`, `{filename}` and `{question_id}` parts are filled with
the real values when the frontend makes the request. Some routes also
accept query values, such as a subject filter, marks filter or page
number.

### Login and protected requests

After a successful login, the backend returns a JWT access token. The
frontend keeps this token in `sessionStorage` for the current browser
session and sends it with protected API requests.

``` mermaid
sequenceDiagram
    actor User
    participant UI as Frontend
    participant API as Backend
    participant DB as SQLite

    User->>UI: Enters login details
    UI->>API: POST /auth/login
    API->>DB: Check user and password
    DB-->>API: User record
    API-->>UI: access_token and token_type
    UI->>UI: Keep token for this session
    UI->>API: Request with Bearer token
    API->>DB: Check user and permissions
    DB-->>API: Permission information
    API-->>UI: Requested data as JSON
```

## 10. JSON: the shape of information

JSON is a simple text format for sending organised information. It uses
names and values.

For example, a subject might look like this:

``` json
{
  "subject_id": "operating-systems",
  "name": "Operating Systems",
  "status": "active"
}
```

A simplified question record might look like this:

``` json
{
  "question_id": "stable-question-id",
  "subject_id": "operating-systems",
  "question_number": "Q1(a)",
  "text": "Explain the process scheduling methods.",
  "marks": 5,
  "section_name": "Section A",
  "source_pdf": "operating-systems-paper.pdf",
  "page_number": 2,
  "assets": [],
  "tables": [],
  "priority": 1,
  "status": "pending"
}
```

A simplified processing-job response might look like this:

``` json
{
  "job_id": "job-123",
  "subject": "Operating Systems",
  "filename": "operating-systems-paper.pdf",
  "status": "running",
  "message": "Document is being processed"
}
```

A simplified list response might look like this:

``` json
{
  "items": [
    {
      "question_id": "question-001",
      "text": "What is an operating system?",
      "marks": 2
    },
    {
      "question_id": "question-002",
      "text": "Explain process scheduling.",
      "marks": 5
    }
  ],
  "page": 1,
  "page_size": 20,
  "total": 2
}
```

These are **easy-to-read examples**, not a promise that every API
response has exactly these fields. The actual fields depend on the API
route and its response model.

### How JSON travels through the application

``` mermaid
flowchart LR
    A[Frontend action] --> B[HTTP request]
    B --> C[Backend API route]
    C --> D[Read or update SQLite]
    D --> E[Build response data]
    E --> F[Convert to JSON]
    F --> G[Send API response]
    G --> H[Frontend reads fields]
    H --> I[Update screen]
```

### Example: asking for questions

The frontend might request:

``` http
GET /api/question-bank?subject_id=operating-systems&page=1&page_size=20
```

The backend reads the matching questions from SQLite and returns a JSON
response. The frontend reads the returned question text, marks and other
fields, then displays them in the Question Bank table.

### Example: sending an update

When a user edits a question, the frontend can send a request like:

``` http
PATCH /api/question-bank/question-001
Content-Type: application/json
```

With a JSON body such as:

``` json
{
  "text": "Explain the role of an operating system.",
  "marks": 5
}
```

The backend checks the values, updates the saved record and sends back
the result.

## 11. From Question Bank to a new question paper

The Question Bank is the organised collection of questions. Paper
generation uses those saved questions and the rules in a blueprint.

``` mermaid
flowchart TD
    A[Saved Question Bank] --> C[Paper generation service]
    B[Blueprint: sections, marks and rules] --> C
    C --> D[Select suitable questions]
    D --> E[Arrange questions into sections]
    E --> F[Create DOCX paper]
    F --> G[Save paper and its details in SQLite]
    G --> H[Frontend shows or downloads paper]
```

A blueprint tells the application how a paper should be arranged. The
generation service uses the blueprint and Question Bank records to
prepare the paper.

## 12. Security and safe handling

A few simple checks help protect the application:

-   Users sign in before using protected features.
-   The backend checks the access token and user permissions.
-   Admin-only actions are restricted to users with the required role.
-   Uploaded files are checked before they are accepted.
-   File and question operations are handled by the backend, not by
    direct database access from the frontend.
-   The frontend stores the login token for the current session rather
    than treating the browser as the main data store.

## 13. The three development phases

The work was completed in three broad phases.

### Phase 1 --- Build the first screens

The first step was to create the initial UI pages using the team's
general requirements. At this stage, the focus was on the layout,
navigation, forms, buttons and the overall user journey. The screens
used sample or placeholder information where the real backend connection
was not yet available.

### Phase 2 --- Connect the screens to the sample backend

The next step was to connect the frontend to the sample backend and the
API routes supplied by the team. The UI started requesting real data,
sending user actions and displaying the responses. This phase helped
connect the visible screens with the backend's actual operations.

### Phase 3 --- Make the final corrections

After discussing the features and finalising how the UI should work, the
screens and their behaviour were corrected. The aim was to make the
pages, API calls and displayed information match the agreed features and
final user flow.

``` mermaid
flowchart LR
    A[Phase 1: Initial UI pages] --> B[Phase 2: Connect sample backend APIs]
    B --> C[Phase 3: Final UI and feature corrections]
    C --> D[Frontend and backend work together]
```

## 14. Quick glossary

  -----------------------------------------------------------------------
  Word                                Simple meaning
  ----------------------------------- -----------------------------------
  Frontend                            The screens the user sees

  Backend                             The part that does the work

  API                                 A way for the frontend to ask the
                                      backend for something

  Request                             A message asking for an action or
                                      information

  Response                            The answer sent back

  JSON                                A neat way to arrange and send
                                      information

  SQLite                              The database where the application
                                      keeps its records

  BLOB                                The place in a database record
                                      where file bytes can be stored

  Scrapper                            The old local tool that collects
                                      text, pages and images from
                                      documents

  Parser                              The tool that understands and
                                      organises content into questions

  OCR                                 A way for a computer to read words
                                      from an image

  Nemotron                            NVIDIA's models used by the active
                                      Lite document-reading flow

  LangGraph                           A tool that connects steps in the
                                      legacy local parsing flow

  Pydantic                            A tool that checks whether data has
                                      the expected shape

  Question Bank                       The organised collection of
                                      extracted questions

  Blueprint                           The rules for arranging a new
                                      question paper

  Job                                 A record that tracks a task that
                                      may take time
  -----------------------------------------------------------------------

## 15. Final picture

The user works with the frontend. The frontend sends requests to the
backend. The backend checks the request, runs the required service, uses
the NVIDIA Lite document-processing path for active parsing, and saves
application data in SQLite. The Question Bank keeps the extracted
questions organised. Paper generation uses those questions and a
blueprint to create a new paper. The backend then sends JSON or a file
back to the frontend so the user can see or download the result.

The main idea is simple:

**Frontend shows it. Backend handles it. Parser organises it. SQLite
keeps it. APIs connect everything.**
