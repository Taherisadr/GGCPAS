from flask import Flask, request, render_template, jsonify, send_file
from markupsafe import Markup
import os
import fitz  # PyMuPDF
import requests
import json
import traceback
import io
from xhtml2pdf import pisa

app = Flask(__name__)
UPLOAD_FOLDER = 'uploads'
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

OPENROUTER_API_KEY = "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"
MODEL = "mistralai/mistral-7b-instruct"
HEADERS = {
    "Authorization": f"Bearer {OPENROUTER_API_KEY}",
    "Content-Type": "application/json"
}

def extract_text(pdf_path):
    doc = fitz.open(pdf_path)
    return "\n".join([page.get_text() for page in doc])

def parse_w2_1099(text):
    # Basic placeholder parsing logic
    data = {}
    if "W-2" in text:
        data['form_type'] = "W-2"
        data['wages'] = extract_amount(text, r'Box 1.*?\$?([\d,]+\.\d{2})')
        data['federal_tax'] = extract_amount(text, r'Box 2.*?\$?([\d,]+\.\d{2})')
    elif "1099" in text:
        data['form_type'] = "1099"
        data['amount'] = extract_amount(text, r'Box 1.*?\$?([\d,]+\.\d{2})')
    else:
        data['form_type'] = "Unknown"
    return data

def extract_amount(text, pattern):
    import re
    match = re.search(pattern, text, re.IGNORECASE)
    return match.group(1) if match else "Not Found"

def extract_fields_with_llm(text):
    system_message = (
        "You are a helpful AI assistant that extracts structured tax information from a W-2 or 1099 form text. "
        "Return only a valid JSON object with the following keys if present: "
        "For W-2: Employee Name, Employer Name, Wages (Box 1), Federal Income Tax Withheld (Box 2), Social Security Wages (Box 3), Filing Year. "
        "For 1099: Recipient Name, Payer Name, Nonemployee Compensation (Box 1), Federal Income Tax Withheld (Box 4), Filing Year. "
        "Do not include any commentary or explanation—only valid JSON."
    )
    user_message = f"""
Here is the form text:\n\n{text[:2000]}\n\nExtract the fields and respond only with JSON.
"""
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system_message},
            {"role": "user", "content": user_message}
        ]
    }
    try:
        response = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers=HEADERS,
            json=payload,
            timeout=30
        )
        if response.status_code == 200:
            reply = response.json()["choices"][0]["message"]["content"]
            import re, json as pyjson
            match = re.search(r'\{.*\}', reply, re.DOTALL)
            if match:
                json_str = match.group()
                try:
                    parsed = pyjson.loads(json_str)
                    return parsed
                except Exception:
                    return {"error": "Failed to parse JSON from LLM."}
            else:
                return {"error": "No JSON object found in LLM reply."}
        else:
            return {"error": f"OpenRouter API failed: {response.status_code}"}
    except Exception as e:
        return {"error": str(e)}

def compute_tax_1040(data, extra):
    # Helper to safely get float from string
    def safe_float(val):
        try:
            return float(str(val).replace(',', '').replace('$', '').strip())
        except Exception:
            return 0.0

    # Filing status
    filing_status = (extra.get('filing_status') or '').lower().replace(' ', '_')
    dependents = int(extra.get('dependents') or 0)
    state_taxes = safe_float(extra.get('state_taxes'))
    other_income = safe_float(extra.get('other_income'))
    adjustments = safe_float(extra.get('adjustments'))
    credits = safe_float(extra.get('credits'))

    # Wages and federal withholding
    wages = safe_float(data.get('Wages (Box 1)', 0))
    federal_withheld = safe_float(data.get('Federal Income Tax Withheld (Box 2)', 0))
    nonemployee_comp = safe_float(data.get('Nonemployee Compensation (Box 1)', 0))
    federal_withheld_1099 = safe_float(data.get('Federal Income Tax Withheld (Box 4)', 0))
    total_income = wages + nonemployee_comp + other_income
    total_fed_withheld = federal_withheld + federal_withheld_1099

    # Standard deduction (2023)
    std_ded = {
        'single': 13850,
        'married_filing_jointly': 27700,
        'married_filing_separate': 13850,
        'head_of_household': 20800
    }.get(filing_status, 13850)
    std_ded += adjustments

    taxable_income = max(0, total_income - std_ded)

    # 2023 tax brackets (single)
    # Expand for other statuses if needed
    def calc_tax(taxable, status):
        if status == 'single':
            if taxable <= 11000:
                return taxable * 0.10
            elif taxable <= 44725:
                return 1100 + (taxable - 11000) * 0.12
            elif taxable <= 95375:
                return 5147 + (taxable - 44725) * 0.22
            else:
                return 16290 + (taxable - 95375) * 0.24
        elif status == 'married_filing_jointly':
            if taxable <= 22000:
                return taxable * 0.10
            elif taxable <= 89450:
                return 2200 + (taxable - 22000) * 0.12
            elif taxable <= 190750:
                return 10294 + (taxable - 89450) * 0.22
            else:
                return 32580 + (taxable - 190750) * 0.24
        elif status == 'married_filing_separate':
            if taxable <= 11000:
                return taxable * 0.10
            elif taxable <= 44725:
                return 1100 + (taxable - 11000) * 0.12
            elif taxable <= 95375:
                return 5147 + (taxable - 44725) * 0.22
            else:
                return 16290 + (taxable - 95375) * 0.24
        else:
            # Default to single
            if taxable <= 11000:
                return taxable * 0.10
            elif taxable <= 44725:
                return 1100 + (taxable - 11000) * 0.12
            elif taxable <= 95375:
                return 5147 + (taxable - 44725) * 0.22
            else:
                return 16290 + (taxable - 95375) * 0.24

    est_tax = calc_tax(taxable_income, filing_status)
    est_tax = max(0, est_tax - credits)
    refund_or_due = round(total_fed_withheld - est_tax, 2)
    status_msg = "You will receive a refund." if refund_or_due > 0 else "You owe additional taxes."

    summary = {
        "Filing Status": extra.get('filing_status', ''),
        "Dependents": dependents,
        "Total Income": round(total_income, 2),
        "Standard Deduction + Adjustments": round(std_ded, 2),
        "Taxable Income": round(taxable_income, 2),
        "Estimated Tax Owed": round(est_tax, 2),
        "Tax Withheld": round(total_fed_withheld, 2),
        "Refund or Amount Due": refund_or_due,
        "Status Message": status_msg
    }

    # HTML Form 1040 (simplified, not official)
    html_1040 = f'''
    <div style="background:#fff; border-radius:10px; padding:24px; max-width:700px; margin:30px auto; box-shadow:0 2px 8px rgba(0,0,0,0.08);">
    <h2 style="text-align:center;">Form 1040 (U.S. Individual Income Tax Return)</h2>
    <table style="width:100%; font-size:1.1em;">
      <tr><td><b>Filing Status:</b></td><td>{summary['Filing Status']}</td></tr>
      <tr><td><b>Dependents:</b></td><td>{summary['Dependents']}</td></tr>
      <tr><td><b>Total Income:</b></td><td>${summary['Total Income']}</td></tr>
      <tr><td><b>Standard Deduction + Adjustments:</b></td><td>${summary['Standard Deduction + Adjustments']}</td></tr>
      <tr><td><b>Taxable Income:</b></td><td>${summary['Taxable Income']}</td></tr>
      <tr><td><b>Estimated Tax Owed:</b></td><td>${summary['Estimated Tax Owed']}</td></tr>
      <tr><td><b>Tax Withheld:</b></td><td>${summary['Tax Withheld']}</td></tr>
      <tr><td><b>Credits:</b></td><td>${credits}</td></tr>
      <tr><td><b>Refund or Amount Due:</b></td><td><b>${summary['Refund or Amount Due']}</b></td></tr>
    </table>
    <h3 style="text-align:center; color:#28a745; margin-top:24px;">{summary['Status Message']}</h3>
    </div>
    '''
    return summary, html_1040

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/upload', methods=['POST'])
def upload_file():
    try:
        if 'pdf1' not in request.files or 'pdf2' not in request.files:
            return jsonify({'error': 'Please upload two PDF files.'}), 400
        file1 = request.files['pdf1']
        file2 = request.files['pdf2']
        if file1.filename == '' or file2.filename == '':
            return jsonify({'error': 'No selected file(s)'}), 400
        if file1 and file2:
            filepath1 = os.path.join(app.config['UPLOAD_FOLDER'], file1.filename)
            filepath2 = os.path.join(app.config['UPLOAD_FOLDER'], file2.filename)
            file1.save(filepath1)
            file2.save(filepath2)
            print(f"Saved files: {filepath1}, {filepath2}")
            text1 = extract_text(filepath1)
            text2 = extract_text(filepath2)
            print(f"Extracted text from both PDFs.")
            # Use LLM to extract fields
            data1 = extract_fields_with_llm(text1)
            print(f"LLM extraction for doc1: {data1}")
            data2 = extract_fields_with_llm(text2)
            print(f"LLM extraction for doc2: {data2}")
            return jsonify({'doc1': data1, 'doc2': data2})
    except Exception as e:
        tb = traceback.format_exc()
        print(f"Error in /upload: {e}\n{tb}")
        return jsonify({'error': f'Exception: {str(e)}', 'traceback': tb}), 500

@app.route('/chat', methods=['POST'])
def chat():
    data = request.get_json()
    question = data.get('question', '')
    extracted_data = data.get('extracted_data', {})
    tax_summary = data.get('tax_summary', {})
    extra_fields = data.get('extra_fields', {})
    form_1040 = data.get('form_1040', {})

    context = {
        "Extracted W-2/1099 Data": extracted_data,
        "Tax Summary": tax_summary,
        "User Provided Info": extra_fields,
        "Form 1040 Summary": form_1040
    }

    system_prompt = (
        "You are an AI tax expert helping the user understand their tax situation. "
        "You have access to their W-2/1099 data, tax summary, user-provided information (such as filing status, dependents, state/local taxes, other income, adjustments, and credits), and their processed Form 1040 summary. "
        "Answer clearly and accurately based on all available information."
    )

    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"User tax data:\n{json.dumps(context, indent=2)}"},
            {"role": "user", "content": f"User question: {question}"}
        ]
    }

    try:
        response = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers=HEADERS,
            json=payload,
            timeout=30
        )
        if response.status_code == 200:
            answer = response.json()["choices"][0]["message"]["content"]
            return jsonify({"answer": answer})
        else:
            return jsonify({"error": f"OpenRouter API error: {response.status_code} - {response.text}"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/calculate', methods=['POST'])
def calculate():
    data = request.get_json()
    merged_data = data.get('merged_data', {})
    extra_fields = data.get('extra_fields', {})
    summary, html_1040 = compute_tax_1040(merged_data, extra_fields)
    return jsonify({'summary': summary, 'html_1040': html_1040})

'''
if __name__ == '__main__':
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
    print('Starting Flask app...')
    app.run(debug=True, port=8080)
'''


if __name__ == '__main__':
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
    port = int(os.environ.get("PORT", 8080))
    print(f'Starting Flask app on port {port}...')
    app.run(host='0.0.0.0', port=port)
