
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi.requests import Request
from pydantic import BaseModel, EmailStr
from io import BytesIO
from email.message import EmailMessage
import html
import mimetypes
import os
import re
import smtplib
import ssl

from pypdf import PdfReader
from PIL import Image
import pytesseract
from pdf2image import convert_from_bytes

app = FastAPI(title="Boltian Comparador V2")
templates = Jinja2Templates(directory="templates")

MAX_FILE_SIZE = 12 * 1024 * 1024
ALLOWED_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png", ".webp"}

# IMPORTANTE:
# Precios SIN impuestos. Revisarlos antes de producción y cuando cambien las ofertas.
TARIFAS = [
    {"company": "Gana Energía", "plan": "Tarifa 24 horas", "energy": 0.1190, "p1": 0.0890, "p2": 0.0890, "service_month": 0.0},
    {"company": "Nordy", "plan": "Tarifa 24H", "energy": 0.1190, "p1": 0.0890, "p2": 0.0890, "service_month": 0.0},
    {"company": "Naturgy", "plan": "Tarifa Por Uso Luz", "energy": 0.1120, "p1": 0.123030, "p2": 0.061562, "service_month": 0.0},
    {"company": "Endesa", "plan": "Tarifa Conecta Luz", "energy": 0.1090, "p1": 2.849 * 12 / 365, "p2": 2.849 * 12 / 365, "service_month": 0.0},
    {"company": "Iberdrola", "plan": "Plan Online", "energy": 0.1249, "p1": 0.119151, "p2": 0.069836, "service_month": 0.0},
    {"company": "Repsol", "plan": "Tarifa Sin Horarios", "energy": 0.109650, "p1": 0.090137, "p2": 0.090137, "service_month": 4.99},
]

IEE = 0.0511269632
IVA = 0.21


class CompareInput(BaseModel):
    consumo_kwh: float
    dias: int
    potencia_p1: float
    potencia_p2: float
    total_factura: float
    comercializadora_actual: str | None = None


def clean_num(value):
    if value is None:
        return None
    s = str(value).strip().replace("€", "").replace("kWh", "").replace("kW", "")
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    try:
        return float(re.sub(r"[^0-9.\-]", "", s))
    except Exception:
        return None


def safe_filename(filename: str) -> str:
    filename = os.path.basename(filename or "factura")
    filename = re.sub(r"[^A-Za-z0-9._-]+", "_", filename)
    return filename[:120] or "factura"


def validate_file(filename: str, data: bytes):
    ext = os.path.splitext((filename or "").lower())[1]
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(400, "Formato no permitido. Usa PDF, JPG, PNG o WEBP.")
    if not data:
        raise HTTPException(400, "El archivo está vacío.")
    if len(data) > MAX_FILE_SIZE:
        raise HTTPException(413, "La factura supera el límite de 12 MB.")


def extract_pdf_text(data: bytes) -> str:
    text = ""
    try:
        reader = PdfReader(BytesIO(data))
        for page in reader.pages:
            text += "\n" + (page.extract_text() or "")
    except Exception:
        return ""
    return text.strip()


def ocr_image(img: Image.Image) -> str:
    return pytesseract.image_to_string(img, lang="spa+eng")


def extract_text(data: bytes, content_type: str, filename: str) -> str:
    ext = os.path.splitext((filename or "").lower())[1]
    if ext == ".pdf" or content_type == "application/pdf":
        text = extract_pdf_text(data)
        if len(re.sub(r"\s+", "", text)) < 120:
            try:
                pages = convert_from_bytes(data, dpi=200, first_page=1, last_page=3)
                text = "\n".join(ocr_image(page) for page in pages)
            except Exception:
                pass
        return text

    try:
        return ocr_image(Image.open(BytesIO(data)).convert("RGB"))
    except Exception as exc:
        raise HTTPException(400, f"No se pudo leer la imagen: {exc}")


def first(patterns, text):
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.I | re.S)
        if match:
            return clean_num(match.group(1))
    return None


def extract_fields(text: str):
    t = re.sub(r"[ \t]+", " ", text)
    upper = t.upper()

    consumo = first([
        r"consumo(?:\s+total|\s+facturado|\s+de\s+energ[ií]a)?[^0-9]{0,45}([\d\.,]+)\s*kWh",
        r"energ[ií]a\s+consumida[^0-9]{0,45}([\d\.,]+)\s*kWh",
        r"consumo\s+en\s+el\s+periodo[^0-9]{0,45}([\d\.,]+)\s*kWh",
    ], t)

    if not consumo:
        vals = []
        for match in re.finditer(
            r"(?:P[123]|punta|llano|valle)[^0-9]{0,35}([\d\.,]+)\s*kWh",
            t,
            flags=re.I,
        ):
            value = clean_num(match.group(1))
            if value and 0 < value < 100000:
                vals.append(value)
        if 2 <= len(vals) <= 6:
            consumo = sum(vals[:3])

    pvals = []
    for match in re.finditer(
        r"potencia(?:\s+contratada)?[^0-9]{0,50}([\d\.,]+)\s*kW",
        t,
        flags=re.I,
    ):
        value = clean_num(match.group(1))
        if value and 0.1 <= value <= 30:
            pvals.append(value)

    p1 = pvals[0] if pvals else None
    p2 = pvals[1] if len(pvals) > 1 else p1

    total = first([
        r"total\s+(?:factura|a\s+pagar|importe)[^0-9]{0,35}([\d\.,]+)\s*€",
        r"importe\s+total[^0-9]{0,35}([\d\.,]+)\s*€",
        r"total[^0-9]{0,20}([\d\.,]+)\s*€",
    ], t)

    dias = first([
        r"(?:n[uú]mero\s+de\s+d[ií]as|d[ií]as\s+facturados)[^0-9]{0,20}(\d{1,3})",
        r"periodo[^0-9]{0,60}(\d{1,3})\s*d[ií]as",
    ], t)
    dias = int(dias) if dias and 1 <= dias <= 120 else None

    providers = [
        "GANA ENERGÍA", "GANA ENERGIA", "NORDY",
        "NATURGY", "ENDESA", "IBERDROLA", "REPSOL"
    ]
    current = next((provider.title() for provider in providers if provider in upper), None)
    if current == "Gana Energia":
        current = "Gana Energía"

    return {
        "consumo_kwh": round(consumo, 2) if consumo else None,
        "dias": dias or 30,
        "potencia_p1": round(p1, 3) if p1 else None,
        "potencia_p2": round(p2, 3) if p2 else None,
        "total_factura": round(total, 2) if total else None,
        "comercializadora_actual": current,
        "confidence": {
            "consumo_kwh": bool(consumo),
            "dias": bool(dias),
            "potencia_p1": bool(p1),
            "potencia_p2": bool(p2),
            "total_factura": bool(total),
        },
    }


def annual_cost(tarifa, annual_kwh, p1, p2):
    energy = annual_kwh * tarifa["energy"]
    power = 365 * (p1 * tarifa["p1"] + p2 * tarifa["p2"])
    service = 12 * tarifa.get("service_month", 0)
    base = energy + power + service
    return base * (1 + IEE) * (1 + IVA)


def calculate_comparison(d: CompareInput):
    if (
        d.consumo_kwh <= 0
        or d.dias <= 0
        or d.potencia_p1 <= 0
        or d.potencia_p2 <= 0
        or d.total_factura <= 0
    ):
        raise HTTPException(400, "Los datos de factura no son válidos.")

    annual_kwh = d.consumo_kwh * 365 / d.dias
    current_annual = d.total_factura * 365 / d.dias

    rows = []
    for tarifa in TARIFAS:
        cost = annual_cost(tarifa, annual_kwh, d.potencia_p1, d.potencia_p2)
        rows.append({
            **tarifa,
            "coste_anual": round(cost, 2),
            "coste_mensual": round(cost / 12, 2),
            "ahorro_anual": round(current_annual - cost, 2),
        })

    rows.sort(key=lambda row: row["coste_anual"])
    return {
        "consumo_anual_estimado": round(annual_kwh, 1),
        "coste_anual_actual": round(current_annual, 2),
        "mejor_ahorro": round(rows[0]["ahorro_anual"], 2),
        "ofertas": rows,
    }


def send_lead_email(
    customer_name: str,
    customer_phone: str,
    customer_email: str,
    current_company: str,
    selected_company: str,
    selected_plan: str,
    selected_saving: float,
    selected_cost_month: float,
    consumo_kwh: float,
    dias: int,
    p1: float,
    p2: float,
    total_factura: float,
    invoice_filename: str,
    invoice_bytes: bytes,
    invoice_content_type: str,
):
    smtp_host = os.getenv("SMTP_HOST")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_user = os.getenv("SMTP_USER")
    smtp_password = os.getenv("SMTP_PASSWORD")
    smtp_from = os.getenv("SMTP_FROM", smtp_user or "clientes@boltian.es")
    lead_to = os.getenv("LEAD_TO", "clientes@boltian.es")
    smtp_use_ssl = os.getenv("SMTP_USE_SSL", "false").lower() == "true"

    if not smtp_host or not smtp_user or not smtp_password:
        raise HTTPException(
            503,
            "El envío de leads todavía no está configurado en el servidor."
        )

    subject = (
        f"Nuevo lead Boltian | {selected_company} | "
        f"Ahorro {selected_saving:.0f} €/año"
    )

    plain = f"""NUEVO LEAD - COMPARADOR BOLTIAN

CLIENTE
Nombre: {customer_name}
Teléfono: {customer_phone}
Email: {customer_email}

FACTURA ACTUAL
Comercializadora: {current_company or "No detectada"}
Consumo del periodo: {consumo_kwh:.2f} kWh
Días facturados: {dias}
Potencia P1: {p1:.3f} kW
Potencia P2: {p2:.3f} kW
Importe factura actual: {total_factura:.2f} €

OFERTA ELEGIDA
Compañía: {selected_company}
Tarifa: {selected_plan}
Coste mensual estimado: {selected_cost_month:.2f} €
Ahorro anual estimado: {selected_saving:.2f} €

La factura original se adjunta a este correo.
"""

    body_html = f"""
    <html>
      <body style="font-family:Arial,sans-serif;color:#172235">
        <h2 style="margin-bottom:4px">Nuevo lead del comparador Boltian</h2>
        <p style="color:#66758a">El cliente ha seleccionado una oferta desde boltian.es.</p>

        <h3>Cliente</h3>
        <p>
          <b>Nombre:</b> {html.escape(customer_name)}<br>
          <b>Teléfono:</b> {html.escape(customer_phone)}<br>
          <b>Email:</b> {html.escape(customer_email)}
        </p>

        <h3>Factura actual</h3>
        <p>
          <b>Comercializadora:</b> {html.escape(current_company or "No detectada")}<br>
          <b>Consumo:</b> {consumo_kwh:.2f} kWh ({dias} días)<br>
          <b>Potencia:</b> P1 {p1:.3f} kW · P2 {p2:.3f} kW<br>
          <b>Importe:</b> {total_factura:.2f} €
        </p>

        <div style="padding:16px;border-radius:12px;background:#eefbf3">
          <h3 style="margin-top:0">Oferta elegida</h3>
          <b>{html.escape(selected_company)} — {html.escape(selected_plan)}</b><br>
          Coste estimado: {selected_cost_month:.2f} €/mes<br>
          <b style="color:#138a46">Ahorro estimado: {selected_saving:.2f} €/año</b>
        </div>

        <p style="font-size:12px;color:#77869a;margin-top:20px">
          La factura enviada por el cliente va adjunta. El comparador no la almacena
          de forma permanente después de procesar esta solicitud.
        </p>
      </body>
    </html>
    """

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = smtp_from
    msg["To"] = lead_to
    msg["Reply-To"] = customer_email
    msg.set_content(plain)
    msg.add_alternative(body_html, subtype="html")

    guessed_type, _ = mimetypes.guess_type(invoice_filename)
    attachment_type = invoice_content_type or guessed_type or "application/octet-stream"
    if "/" in attachment_type:
        maintype, subtype = attachment_type.split("/", 1)
    else:
        maintype, subtype = "application", "octet-stream"

    msg.add_attachment(
        invoice_bytes,
        maintype=maintype,
        subtype=subtype,
        filename=safe_filename(invoice_filename),
    )

    context = ssl.create_default_context()

    try:
        if smtp_use_ssl:
            with smtplib.SMTP_SSL(
                smtp_host, smtp_port, timeout=20, context=context
            ) as server:
                server.login(smtp_user, smtp_password)
                server.send_message(msg)
        else:
            with smtplib.SMTP(smtp_host, smtp_port, timeout=20) as server:
                server.ehlo()
                server.starttls(context=context)
                server.ehlo()
                server.login(smtp_user, smtp_password)
                server.send_message(msg)
    except Exception as exc:
        print("SMTP ERROR:", repr(exc))
        raise HTTPException(
            502,
            "No se pudo enviar la solicitud. Inténtalo de nuevo en unos minutos."
        )


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse(
        "index.html",
        {"request": request, "tarifas": TARIFAS},
    )


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/api/tarifas")
def tarifas():
    return TARIFAS


@app.post("/api/analyze")
async def analyze(file: UploadFile = File(...)):
    data = await file.read()
    validate_file(file.filename or "", data)

    text = extract_text(
        data,
        file.content_type or "",
        file.filename or "",
    )
    if not text.strip():
        raise HTTPException(
            422,
            "No se ha podido leer esta factura. Puedes introducir los datos manualmente."
        )
    return extract_fields(text)


@app.post("/api/compare")
def compare(d: CompareInput):
    return calculate_comparison(d)


@app.post("/api/lead")
async def lead(
    nombre: str = Form(...),
    telefono: str = Form(...),
    email: EmailStr = Form(...),
    privacidad: bool = Form(...),

    comercializadora_actual: str = Form(""),
    consumo_kwh: float = Form(...),
    dias: int = Form(...),
    potencia_p1: float = Form(...),
    potencia_p2: float = Form(...),
    total_factura: float = Form(...),

    selected_company: str = Form(...),
    selected_plan: str = Form(...),
    selected_saving: float = Form(...),
    selected_cost_month: float = Form(...),

    factura: UploadFile = File(...),
):
    if not privacidad:
        raise HTTPException(
            400,
            "Debes aceptar la política de privacidad para enviar la solicitud."
        )

    nombre = nombre.strip()
    telefono = telefono.strip()

    if len(nombre) < 2 or len(nombre) > 100:
        raise HTTPException(400, "Revisa el nombre.")
    if not re.fullmatch(r"[0-9+()\s.-]{7,25}", telefono):
        raise HTTPException(400, "Revisa el teléfono.")

    valid_offer = next(
        (
            t for t in TARIFAS
            if t["company"] == selected_company and t["plan"] == selected_plan
        ),
        None,
    )
    if not valid_offer:
        raise HTTPException(400, "La oferta seleccionada no es válida.")

    # Recalcula en servidor para que el navegador no pueda falsear el ahorro.
    comparison = calculate_comparison(
        CompareInput(
            consumo_kwh=consumo_kwh,
            dias=dias,
            potencia_p1=potencia_p1,
            potencia_p2=potencia_p2,
            total_factura=total_factura,
            comercializadora_actual=comercializadora_actual,
        )
    )

    server_offer = next(
        (
            offer for offer in comparison["ofertas"]
            if offer["company"] == selected_company
            and offer["plan"] == selected_plan
        ),
        None,
    )
    if not server_offer:
        raise HTTPException(400, "No se ha podido validar la oferta.")

    invoice_bytes = await factura.read()
    validate_file(factura.filename or "", invoice_bytes)

    send_lead_email(
        customer_name=nombre,
        customer_phone=telefono,
        customer_email=str(email),
        current_company=comercializadora_actual,
        selected_company=server_offer["company"],
        selected_plan=server_offer["plan"],
        selected_saving=server_offer["ahorro_anual"],
        selected_cost_month=server_offer["coste_mensual"],
        consumo_kwh=consumo_kwh,
        dias=dias,
        p1=potencia_p1,
        p2=potencia_p2,
        total_factura=total_factura,
        invoice_filename=factura.filename or "factura",
        invoice_bytes=invoice_bytes,
        invoice_content_type=factura.content_type or "",
    )

    return {
        "ok": True,
        "message": "Solicitud enviada correctamente.",
        "selected_company": server_offer["company"],
        "selected_plan": server_offer["plan"],
    }
