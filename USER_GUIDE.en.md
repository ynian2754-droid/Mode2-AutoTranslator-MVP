# Mode2 AutoTranslator — User Guide

Mode2 is a local document translation workbench for Windows. It breaks a source document into traceable units, can prepare optional concept references, translates units in parallel, reviews each saved translation independently, and lets you edit or accept a recorded risk before exporting the document.

This guide covers the normal user workflow. The interface is available in English and Simplified Chinese.

## Contents

- [Before you start](#before-you-start)
- [Install and launch](#install-and-launch)
- [Create or open a project](#create-or-open-a-project)
- [Configure translation and review APIs](#configure-translation-and-review-apis)
- [Import a document](#import-a-document)
- [Prepare concepts (optional)](#prepare-concepts-optional)
- [Translate and review units](#translate-and-review-units)
- [Export the document](#export-the-document)
- [Data and privacy](#data-and-privacy)
- [Troubleshooting](#troubleshooting)
- [Current limits](#current-limits)

## Before you start

You need:

- Windows and Python 3.10 or later with `pip`.
- An OpenAI-compatible API endpoint and model for translation, plus an endpoint and model for review. You may use the same service for both.
- Your own API key when the provider requires authentication. Mode2 does not include a key or model service.

The latest [Windows Release ZIP](https://github.com/ynian2754-droid/Mode2-AutoTranslator-MVP/releases/latest) is the easiest way to get the application files. It is a source package, not a self-contained installer: Python and the project dependencies must be installed separately.

## Install and launch

1. Download and extract the [latest Release ZIP](https://github.com/ynian2754-droid/Mode2-AutoTranslator-MVP/releases/latest).
2. Install Python 3.10 or later with `pip` if it is not already installed.
3. In the extracted project folder, run `安装依赖.bat` once and wait for it to finish.
4. Run `启动.bat`. If the browser does not open automatically, visit <http://127.0.0.1:4873/>.

Keep the terminal window open while using Mode2; closing it stops the local service. If port `4873` is already in use, run `启动.bat 4874` and open <http://127.0.0.1:4874/>.

## Create or open a project

The project library appears after launch.

- To continue work, check the project name and source filename on its card, then open that project.
- To start fresh, choose **New project**, enter a name, and create it.

Projects keep their source copy, translation units, saved translations, review records, and exports together under `book/<project name>/`. Before importing a new source, make sure the intended project is open: importing replaces that project's current source and units. Create a separate project when you want to preserve the previous work.

## Configure translation and review APIs

Open **Settings**. First create reusable **API presets**, then choose which preset each kind of work uses. Every change on the page is saved immediately.

**API presets.** Choose **New preset** and enter a preset name, the provider's OpenAI-compatible base URL, model name, and API key when required. Reasoning effort and temperature depend on what the provider supports; max output tokens and request timeout are under **More parameters**. Use **Load models** if available, or enter the model name yourself. Use **Test connection** in the preset editor before relying on it; the test sends one real request and may cost a little. The preset list is collapsed by default; choose **Show all presets** to edit, duplicate, delete, or see where a preset is used. Editing a preset updates every group and task that uses it. A preset in use can't be deleted until those places use another preset.

**Task assignment.** Choose one preset for each of four groups:

| Group | Work it covers |
| --- | --- |
| Unit translation | First translation and re-translation; expression suggestions |
| Unit review | Automatic independent review after translation; manual rechecks |
| Concept creation | Concepts, meanings, and term candidates from batches of source text |
| Concept verification | Independent candidate checks, rechecks of older concepts, and concept question lookups; related-group disambiguation and per-unit local disambiguation |

Using a different preset (or model) for review than for translation keeps the review independent. Conversation histories are never shared between tasks.

**Advanced settings** let you choose a preset for any of six tasks: unit translation, expression suggestions, unit review, concept generation, concept check, and concept disambiguation. A task's own choice wins; otherwise it follows its group. Each task shows the preset it ends up using and whether it follows its group or has its own choice. Retries of failed concept batches reuse the matching task's preset. Preparing previews, local evidence search, submitting references, manual editing, and exporting itself don't call a model; a concept question lookup calls the Concept check preset only after local evidence is found.

**Use one preset for everything** sets all four groups to the chosen preset and clears task-specific choices after you confirm. It doesn't change the preset itself.

**System prompt presets.** The **System prompts** area keeps a separate library of prompt presets for **Unit translation** and **Unit review**; it is independent from API presets, and the two tasks can't pick each other's presets. Choose **Manage prompts** to open the manager. The built-in default prompt is read-only and identical to earlier versions, so nothing changes until you configure one. You can view its full text and use **Copy as custom preset** to name, edit, and save your own version. Custom presets can be created, edited, renamed, selected, and deleted; a preset in use must be deselected before it can be deleted. A new choice only affects requests started afterwards: first translation and re-translation share the translation prompt, and automatic review and manual rechecks share the review prompt. Custom prompts may break the expected translation format or the review JSON protocol — the editor warns about this, and the output validation and bounded repair stay unchanged. Prompts for concept generation, concept check, concept disambiguation, and expression suggestions remain built in and are not editable.

When upgrading from an earlier version, the old translation and review settings become two presets named “翻译 API” and “检验 API” with the same routing as before, and the old file is backed up to `.runtime/api_settings.v1-backup.json` on the first change.

A key may be left blank only when the configured endpoint explicitly allows unauthenticated requests. Provider compatibility, access, rate limits, and billing are controlled by that provider.

## Import a document

In the translation workspace, choose **Import source file** and select a supported file:

| Import format | Notes |
| --- | --- |
| PDF | Must contain selectable text. Run OCR on scanned pages before importing. |
| EPUB | Text is extracted in the book's reading order. |
| Markdown (`.md`, `.markdown`) | Headings and common Markdown structures are used when splitting the text. |
| TXT | UTF-8 and GB18030 text are supported. |

DOCX import is not supported. Convert a DOCX file to PDF, Markdown, or TXT first. Mode2 can export an editable Word document after translation. Empty files and image-only PDFs without OCR text cannot be imported as useful source text.

Mode2 creates traceable translation units from the imported document. The target unit length is a soft target: the splitter favors complete sentences and document structure, so an individual unit may be shorter or longer than the configured value. For PDFs, page boundaries do not automatically become translation boundaries.

## Prepare concepts (optional)

Concept preparation can help distinguish terms that have different meanings in different parts of a document. It is optional; you can translate without running it.

1. Open the concept page and choose the units to include, or use the whole project when appropriate.
2. Preview the preparation plan and its scope. Previewing does not call a model.
3. Confirm the preparation to send the planned work to your configured services.
4. Review the resulting concepts and source evidence. Use **Manage concepts** to inspect, edit, approve, defer, or reject cards when manual review is available for the project.

Concept candidates can include a proposed meaning, candidate translations, applicable context, common confusions, and source evidence. The independent check contributes a status; it does not replace your judgment. In automatic mode, eligible concepts may be adopted as references after you confirm preparation. **Automatic adoption is not human approval.**

Only references that match a unit are eligible for use. Per-unit limits and manual-reference priority affect what is sent, and the workbench records a reference snapshot for each unit. A prepared card is not guaranteed to be included in every model request. Preparing or changing references does not silently rewrite translations that already exist; reprocess affected units if you want new translations to use updated references.

## Translate and review units

1. In the workspace, select one or more units that are ready to process.
2. Set the concurrency value. It controls how many different units can be processed at the same time. Higher concurrency can trigger provider rate limits.
3. Choose **Start workflow**.

Different units can run in parallel. Within each unit, Mode2 saves the translation before sending it to the separately configured reviewer. The reviewer checks the saved result; it does not automatically approve every translation.

The unit list and detail panel show progress, source text, the saved translation, review status, reviewer findings, and the reference snapshot. Completed work is saved as units finish. Stopping the workflow prevents new units from starting and preserves results already saved.

When a review flags an issue, open the unit and choose the action that fits:

- **Edit and review** — revise the translation, save it, and run the review again.
- **Re-translate** — request a new translation, then review the saved result.
- **Accept risk** — keep the current translation and record that you chose to proceed despite the review result.

Accepting risk is a human decision and remains labeled separately from a passed review. Export uses saved project translations; save or discard any unsaved edits before exporting.

## Export the document

The full-document export becomes available when every unit is either marked as passed or has an explicit accepted-risk decision, and there are no unsaved translation edits.

1. Choose an output format: **Markdown**, **TXT**, **PDF**, **EPUB**, or **Word (.docx)**.
2. Select **Export full document**.
3. The browser downloads the result. Mode2 also stores it in the current project's `output/` folder.

Each export includes a matching `.map.json` trace map that links output nodes and their order to translation units. Export again after editing and reviewing a unit to rebuild the document from the latest saved project data.

Word export is editable and uses an A4 layout with heading, copyright, contents, body, and note styles. Word repaginates the document; its contents text and any printed page numbers remain as translated.

PDF is a readable, reflowed document rather than a visual copy of the source PDF. EPUB export preserves reading order and basic structure, but does not promise to retain every image, complex style, or interactive element.

## Data and privacy

- Project files, source copies, saved translations, review records, and exports are stored locally under `book/<project name>/`.
- API settings and prompt presets are stored locally in `.runtime/api_settings.json`. This file may contain API keys; do not publish it or include it in an issue report. Prompts stay in this file and are not written to project documents, event logs, or exports.
- API keys and model services are not bundled with Mode2.
- When you test an endpoint or run translation, review, or concept preparation, Mode2 sends the content needed for that action to the provider you configured. Depending on the operation, this can include source text or excerpts, adjacent source context, saved translations, candidate concepts, and applicable references.

The provider's own privacy, retention, and billing terms apply to data sent to it. Review those terms before processing sensitive documents.

## Troubleshooting

### The application does not start

Make sure Python 3.10 or later with `pip` is installed, then run `安装依赖.bat` again. Keep the terminal open and read any error shown there.

### Port 4873 is already in use

Start with another port, for example `启动.bat 4874`, then visit <http://127.0.0.1:4874/>.

### The browser says the local service is unavailable

Check that the terminal window is still running the service and that the address uses the same port shown there. The default health page is <http://127.0.0.1:4873/api/health>.

### An API test fails

Check the base URL, model name, and API key. If the provider does not require a key, leave it blank; otherwise verify that the key is valid and has access to the model. A model-list fetch is optional, so you can enter a supported model name manually.

### Requests time out or return a rate-limit error

Reduce concurrency first. If the provider is responding slowly, increase the request timeout in Settings. Provider rate limits and service availability vary.

### A PDF imports with no text

The PDF may contain scanned page images rather than a text layer. Run OCR first, or convert the recognized text to Markdown or TXT and import that file.

### Export is unavailable

Check that each unit is passed or explicitly marked as accepted risk. Save or discard unsaved translation edits, then check the export control again.

## Current limits

- DOCX import and built-in OCR are not available.
- Word export is editable and repaginates content, so it does not promise to match the source PDF's page numbers.
- PDF export reflows content and does not preserve the source page design.
- EPUB export may omit images, complex CSS, or interactive content.
- Translation quality, reviewer behavior, compatibility, cost, and rate limits depend on the API services you configure.

For the Simplified Chinese guide, see [USER_GUIDE.md](USER_GUIDE.md). For the project overview and screenshots, see [README.md](README.md).
