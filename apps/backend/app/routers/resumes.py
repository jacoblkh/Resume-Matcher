"""Resume management endpoints."""

import asyncio
import json
import logging
import re
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, File, HTTPException, Query, UploadFile
from fastapi.responses import Response

from app.database import db
from app.pdf import render_resume_pdf, PDFRenderError
from app.config import settings
from app.services.malware_scanner import scan_file_for_malware  # Hypothetical malware scanning service

logger = logging.getLogger(__name__)
from app.schemas import (
    GenerateContentResponse,
    ImproveResumeRequest,
    ImproveResumeResponse,
    ImproveResumeData,
    ResumeData,
    ResumeFetchData,
    ResumeFetchResponse,
    ResumeListResponse,
    ResumeSummary,
    ResumeUploadResponse,
    RawResume,
    UpdateCoverLetterRequest,
    UpdateOutreachMessageRequest,
    normalize_resume_data,
)
from app.services.parser import parse_document, parse_resume_to_json
from app.services.improver import (
    extract_job_keywords,
    generate_improvements,
    improve_resume,
)
from app.services.cover_letter import (
    generate_cover_letter,
    generate_outreach_message,
)

def _load_config() -> dict:
    """Load configuration from config file."""
    config_path = settings.config_path
    if config_path.exists():
        return json.loads(config_path.read_text())
    return {}

def _load_feature_config() -> dict:
    """Load feature configuration from config file."""
    return _load_config()

def _get_content_language() -> str:
    """Get configured content language from config file."""
    config = _load_config()
    return config.get("content_language", config.get("language", "en"))

router = APIRouter(prefix="/resumes", tags=["Resumes"])

ALLOWED_TYPES = {
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}
MAX_FILE_SIZE = 4 * 1024 * 1024  # 4MB

def is_valid_filename(filename: str) -> bool:
    """Validate the filename to prevent directory traversal attacks."""
    return re.match(r'^[\w,\s-]+\.[A-Za-z]{3}$', filename) is not None

@router.post("/upload", response_model=ResumeUploadResponse)
async def upload_resume(file: UploadFile = File(...)) -> ResumeUploadResponse:
    """Upload and process a resume file (PDF/DOCX)."""
    # Validate file type
    if file.content_type not in ALLOWED_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid file type: {file.content_type}. Allowed: PDF, DOC, DOCX",
        )

    # Validate filename
    if not is_valid_filename(file.filename):
        raise HTTPException(
            status_code=400,
            detail="Invalid filename. Only alphanumeric characters, spaces, and dashes are allowed.",
        )

    # Read and validate size
    content = await file.read()
    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=413,
            detail=f"File too large. Maximum size: {MAX_FILE_SIZE // (1024 * 1024)}MB",
        )

    if len(content) == 0:
        raise HTTPException(status_code=400, detail="Empty file")

    # Scan for malware
    is_safe = await scan_file_for_malware(content)
    if not is_safe:
        raise HTTPException(status_code=400, detail="Uploaded file is potentially malicious.")

    # Convert to markdown
    try:
        markdown_content = await parse_document(content, file.filename or "resume.pdf")
    except Exception as e:
        logger.error(f"Document parsing failed: {e}")
        raise HTTPException(
            status_code=422,
            detail="Failed to parse document. Please ensure it's a valid PDF or DOCX file.",
        )

    # Check if this is the first resume (make it master)
    is_master = db.get_master_resume() is None

    # Store in database first with "processing" status
    resume = db.create_resume(
        content=markdown_content,
        content_type="md",
        filename=file.filename,
        is_master=is_master,
        processed_data=None,
        processing_status="processing",
    )

    # Try to parse to structured JSON (optional, may fail if LLM not configured)
    try:
        processed_data = await parse_resume_to_json(markdown_content)
        db.update_resume(
            resume["resume_id"],
            {
                "processed_data": processed_data,
                "processing_status": "ready",
            },
        )
        resume["processed_data"] = processed_data
        resume["processing_status"] = "ready"
    except Exception as e:
        logger.warning(f"Resume parsing to JSON failed for {file.filename}: {e}")
        db.update_resume(resume["resume_id"], {"processing_status": "failed"})
        resume["processing_status"] = "failed"

    return ResumeUploadResponse(
        message=f"File {file.filename} successfully processed as MD and stored in the DB",
        request_id=str(uuid4()),
        resume_id=resume["resume_id"],
    )

@router.get("", response_model=ResumeFetchResponse)
async def get_resume(resume_id: str = Query(...)) -> ResumeFetchResponse:
    """Fetch resume details by ID."""
    resume = db.get_resume(resume_id)

    if not resume:
        raise HTTPException(status_code=404, detail="Resume not found")

    # Get processing status (default to "pending" for old records)
    processing_status = resume.get("processing_status", "pending")

    # Build response
    raw_resume = RawResume(
        id=None,
        content=resume["content"],
        content_type=resume["content_type"],
        created_at=resume["created_at"],
        processing_status=processing_status,
    )

    # Get processed data if available (no more on-demand parsing)
    processed_data = resume.get("processed_data")

    # Apply lazy migration - add section metadata to old resumes
    if processed_data:
        processed_data = normalize_resume_data(processed_data)

    processed_resume = (
        ResumeData.model_validate(processed_data) if processed_data else None
    )

    return ResumeFetchResponse(
        request_id=str(uuid4()),
        data=ResumeFetchData(
            resume_id=resume_id,
            raw_resume=raw_resume,
            processed_resume=processed_resume,
            cover_letter=resume.get("cover_letter"),
            outreach_message=resume.get("outreach_message"),
            parent_id=resume.get("parent_id"),
        ),
    )

@router.get("/list", response_model=ResumeListResponse)
async def list_resumes(include_master: bool = Query(False)) -> ResumeListResponse:
    """List resumes, optionally including the master resume."""
    resumes = db.list_resumes()
    if not include_master:
        resumes = [resume for resume in resumes if not resume.get("is_master", False)]

    resumes.sort(key=lambda item: item.get("updated_at", ""), reverse=True)

    summaries = [
        ResumeSummary(
            resume_id=resume["resume_id"],
            filename=resume.get("filename"),
            is_master=resume.get("is_master", False),
            parent_id=resume.get("parent_id"),
            processing_status=resume.get("processing_status", "pending"),
            created_at=resume.get("created_at", ""),
            updated_at=resume.get("updated_at", ""),
        )
        for resume in resumes
    ]

    return ResumeListResponse(request_id=str(uuid4()), data=summaries)

@router.post("/improve", response_model=ImproveResumeResponse)
async def improve_resume_endpoint(
    request: ImproveResumeRequest,
) -> ImproveResumeResponse:
    """Improve/tailor a resume for a specific job description."""
    # Fetch resume
    resume = db.get_resume(request.resume_id)
    if not resume:
        raise HTTPException(status_code=404, detail="Resume not found")

    # Fetch job description
    job = db.get_job(request.job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job description not found")

    # Load feature configuration and content language
    feature_config = _load_feature_config()
    enable_cover_letter = feature_config.get("enable_cover_letter", False)
    enable_outreach = feature_config.get("enable_outreach_message", False)
    language = _get_content_language()

    try:
        # Extract keywords from job description
        job_keywords = await extract_job_keywords(job["content"])

        # Generate improved resume in the configured language
        improved_data = await improve_resume(
            original_resume=resume["content"],
            job_description=job["content"],
            job_keywords=job_keywords,
            language=language,
        )

        # Convert improved data to JSON string for storage
        improved_text = json.dumps(improved_data, indent=2)

        # Generate improvement suggestions
        improvements = generate_improvements(job_keywords)

        # Generate cover letter and outreach message in parallel if enabled
        cover_letter = None
        outreach_message = None

        generation_tasks = []
        if enable_cover_letter:
            generation_tasks.append(
                generate_cover_letter(improved_data, job["content"], language)
            )
        if enable_outreach:
            generation_tasks.append(
                generate_outreach_message(improved_data, job["content"], language)
            )

        if generation_tasks:
            results = await asyncio.gather(*generation_tasks, return_exceptions=True)
            idx = 0
            if enable_cover_letter:
                result = results[idx]
                if not isinstance(result, Exception):
                    cover_letter = result
                else:
                    logger.warning(f"Cover letter generation failed: {result}")
                idx += 1
            if enable_outreach:
                result = results[idx]
                if not isinstance(result, Exception):
                    outreach_message = result
                else:
                    logger.warning(f"Outreach message generation failed: {result}")

        # Store the tailored resume with cover letter and outreach message
        tailored_resume = db.create_resume(
            content=improved_text,
            content_type="json",
            filename=f"tailored_{resume.get('filename', 'resume')}",
            is_master=False,
            parent_id=request.resume_id,
            processed_data=improved_data,
            processing_status="ready",
            cover_letter=cover_letter,
            outreach_message=outreach_message,
        )

        # Store improvement record
        request_id = str(uuid4())
        db.create_improvement(
            original_resume_id=request.resume_id,
            tailored_resume_id=tailored_resume["resume_id"],
            job_id=request.job_id,
            improvements=improvements,
        )

        return ImproveResumeResponse(
            request_id=request_id,
            data=ImproveResumeData(
                request_id=request_id,
                resume_id=tailored_resume["resume_id"],
                job_id=request.job_id,
                resume_preview=ResumeData.model_validate(improved_data),
                improvements=[
                    {
                        "suggestion": imp["suggestion"],
                        "lineNumber": imp.get("lineNumber"),
                    }
                    for imp in improvements
                ],
                markdownOriginal=resume["content"],
                markdownImproved=improved_text,
                cover_letter=cover_letter,
                outreach_message=outreach_message,
            ),
        )

    except Exception as e:
        logger.error(f"Resume improvement failed: {e}")
        raise HTTPException(
            status_code=500,
            detail="Failed to improve resume. Please try again.",
        )

@router.patch("/{resume_id}", response_model=ResumeFetchResponse)
async def update_resume_endpoint(
    resume_id: str, resume_data: ResumeData
) -> ResumeFetchResponse:
    """Update a resume with new structured data."""
    existing = db.get_resume(resume_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Resume not found")

    updated_data = resume_data.model_dump()
    updated_content = json.dumps(updated_data, indent=2)

    updated = db.update_resume(
        resume_id,
        {
            "content": updated_content,
            "content_type": "json",
            "processed_data": updated_data,
            "processing_status": "ready",
        },
    )

    if not updated:
        raise HTTPException(status_code=500, detail="Failed to update resume")

    raw_resume = RawResume(
        id=None,
        content=updated["content"],
        content_type=updated["content_type"],
        created_at=updated["created_at"],
        processing_status=updated.get("processing_status", "pending"),
    )

    processed_resume = (
        ResumeData.model_validate(updated.get("processed_data"))
        if updated.get("processed_data")
        else None
    )

    return ResumeFetchResponse(
        request_id=str(uuid4()),
        data=ResumeFetchData(
            resume_id=resume_id,
            raw_resume=raw_resume,
            processed_resume=processed_resume,
        ),
    )

@router.get("/{resume_id}/pdf")
async def download_resume_pdf(
    resume_id: str,
    template: str = Query("swiss-single"),
    pageSize: str = Query("A4", pattern="^(A4|LETTER)$"),
    marginTop: int = Query(10, ge=5, le=25),
    marginBottom: int = Query(10, ge=5, le=25),
    marginLeft: int = Query(10, ge=5, le=25),
    marginRight: int = Query(10, ge=5, le=25),
    sectionSpacing: int = Query(3, ge=1, le=5),
    itemSpacing: int = Query(2, ge=1, le=5),
    lineHeight: int = Query(3, ge=1, le=5),
    fontSize: int = Query(3, ge=1, le=5),
    headerScale: int = Query(3, ge=1, le=5),
    headerFont: str = Query("serif", pattern="^(serif|sans-serif|mono)$"),
    bodyFont: str = Query("sans-serif", pattern="^(serif|sans-serif|mono)$"),
    compactMode: bool = Query(False),
    showContactIcons: bool = Query(False),
    accentColor: str = Query("blue", pattern="^(blue|green|orange|red)$"),
) -> Response:
    """Generate a PDF for a resume using headless Chromium."""
    resume = db.get_resume(resume_id)
    if not resume:
        raise HTTPException(status_code=404, detail="Resume not found")

    # Build print URL with all settings
    params = (
        f"template={template}"
        f"&pageSize={pageSize}"
        f"&marginTop={marginTop}"
        f"&marginBottom={marginBottom}"
        f"&marginLeft={marginLeft}"
        f"&marginRight={marginRight}"
        f"&sectionSpacing={sectionSpacing}"
        f"&itemSpacing={itemSpacing}"
        f"&lineHeight={lineHeight}"
        f"&fontSize={fontSize}"
        f"&headerScale={headerScale}"
        f"&headerFont={headerFont}"
        f"&bodyFont={bodyFont}"
        f"&compactMode={str(compactMode).lower()}"
        f"&showContactIcons={str(showContactIcons).lower()}"
        f"&accentColor={accentColor}"
    )
    url = f"{settings.frontend_base_url}/print/resumes/{resume_id}?{params}"

    # Use the exact margins provided; compact mode only affects spacing.
    pdf_margins = {
        "top": marginTop,
        "right": marginRight,
        "bottom": marginBottom,
        "left": marginLeft,
    }

    # Render PDF with margins applied to every page
    try:
        pdf_bytes = await render_resume_pdf(url, pageSize, margins=pdf_margins)
    except PDFRenderError as e:
        raise HTTPException(status_code=503, detail=str(e))

    headers = {"Content-Disposition": f'attachment; filename="resume_{resume_id}.pdf"'}
    return Response(content=pdf_bytes, media_type="application/pdf", headers=headers)

@router.delete("/{resume_id}")
async def delete_resume(resume_id: str) -> dict:
    """Delete a resume by ID."""
    if not db.delete_resume(resume_id):
        raise HTTPException(status_code=404, detail="Resume not found")

    return {"message": "Resume deleted successfully"}

@router.patch("/{resume_id}/cover-letter")
async def update_cover_letter(
    resume_id: str, request: UpdateCoverLetterRequest
) -> dict:
    """Update the cover letter for a resume."""
    resume = db.get_resume(resume_id)
    if not resume:
        raise HTTPException(status_code=404, detail="Resume not found")

    db.update_resume(resume_id, {"cover_letter": request.content})
    return {"message": "Cover letter updated successfully"}

@router.patch("/{resume_id}/outreach-message")
async def update_outreach_message(
    resume_id: str, request: UpdateOutreachMessageRequest
) -> dict:
    """Update the outreach message for a resume."""
    resume = db.get_resume(resume_id)
    if not resume:
        raise HTTPException(status_code=404, detail="Resume not found")

    db.update_resume(resume_id, {"outreach_message": request.content})
    return {"message": "Outreach message updated successfully"}

@router.post(
    "/{resume_id}/generate-cover-letter", response_model=GenerateContentResponse
)
async def generate_cover_letter_endpoint(resume_id: str) -> GenerateContentResponse:
    """Generate a cover letter on-demand for an existing tailored resume."""
    resume = db.get_resume(resume_id)
    if not resume:
        raise HTTPException(status_code=404, detail="Resume not found")

    if not resume.get("parent_id"):
        raise HTTPException(
            status_code=400,
            detail="Cover letter can only be generated for tailored resumes. "
            "Please tailor this resume to a job description first.",
        )

    improvement = db.get_improvement_by_tailored_resume(resume_id)
    if not improvement:
        raise HTTPException(
            status_code=400,
            detail="No job context found for this resume. "
            "The resume may have been created before job tracking was implemented.",
        )

    job = db.get_job(improvement["job_id"])
    if not job:
        raise HTTPException(
            status_code=404,
            detail="The associated job description was not found.",
        )

    resume_data = resume.get("processed_data")
    if not resume_data:
        raise HTTPException(
            status_code=400,
            detail="Resume has no processed data. Please re-upload the resume.",
        )

    language = _get_content_language()

    try:
        cover_letter_content = await generate_cover_letter(
            resume_data, job["content"], language
        )
    except Exception as e:
        logger.error(f"Cover letter generation failed: {e}")
        raise HTTPException(
            status_code=500,
            detail="Failed to generate cover letter. Please try again.",
        )

    db.update_resume(resume_id, {"cover_letter": cover_letter_content})

    return GenerateContentResponse(
        content=cover_letter_content