import os
from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
import mysql.connector
from mysql.connector import Error
import imaplib
import email
import pdfplumber
import time
from pathlib import Path
from datetime import datetime
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.edge.options import Options
from PyPDF2 import PdfReader
from langchain.text_splitter import RecursiveCharacterTextSplitter
from langchain_google_genai import GoogleGenerativeAIEmbeddings
import google.generativeai as genai
from langchain_community.vectorstores import FAISS
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain.chains.question_answering import load_qa_chain
from langchain.prompts import PromptTemplate
from dotenv import load_dotenv
import asyncio
import concurrent.futures

app = Flask(__name__)

CORS(app)

# Load environment variables from .env file
load_dotenv()

# Configure the Google Generative AI API with the API key from the environment variable
genai.configure(api_key=os.getenv("GOOGLE_API_KEY"))

def get_db_connection():
    return mysql.connector.connect(
        host='cheetahdb.cx8yyoqogq59.us-east-1.rds.amazonaws.com',  # e.g., 'RDS Endpoint'
        database='cheetah',  # e.g., 'user_db'
        user='CheetahAI_DB',  # e.g., 'root'
        password=os.getenv('PASSWORD'),  # your MySQL password
        autocommit=True
    )

@app.route('/', methods=['GET'])
def index():
    def get_pdf_text(pdf_paths):
        def extract_text(pdf):
            text = ""
            doc = PdfReader(pdf)
            for page in doc.pages:
                text += page.extract_text()
            return text

        with concurrent.futures.ThreadPoolExecutor() as executor:
            texts = executor.map(extract_text, pdf_paths)

        return "".join(texts)

    # Dividing the texts into smaller chunks...
    def get_text_chunks(text):
        text_splitter = RecursiveCharacterTextSplitter(chunk_size=10000, chunk_overlap=1000)
        chunks = text_splitter.split_text(text)
        return chunks

    # Convert these chunks into vectors...
    def get_vector_stores(text_chunks):
        embeddings = GoogleGenerativeAIEmbeddings(model="models/text-embedding-004")
        vector_stores = FAISS.from_texts(text_chunks, embedding=embeddings)
        vector_stores.save_local("faiss-index")

    def get_conversational_chain():
        prompt_template = """
        Answer the question as detailed as possible from the provided context, make sure to provide all the details, if the answer is not in
        provided context just say, "n/a", don't provide the wrong answer\n\n
        Context:\n {context}?\n
        Questions: \n{questions}\n
        Answers:
        """
        model = ChatGoogleGenerativeAI(model="gemini-1.5-flash", temperature=0.3)
        prompt = PromptTemplate(template=prompt_template, input_variables=["context", "questions"])
        chain = load_qa_chain(model, chain_type="stuff", prompt=prompt)
        return chain

    # Function to login to email
    def login_to_email(username, password):
        mail = imaplib.IMAP4_SSL('imap.gmail.com')
        mail.login(username, password)
        return mail

    # Function to search for specific emails
    def search_emails(mail, criteria):
        status, messages = mail.search(None, criteria)
        if status == 'OK':
            email_ids = messages[0].split()
            return email_ids
        else:
            return []

    # Function to fetch email by ID
    def fetch_email_by_id(mail, email_id):
        status, msg_data = mail.fetch(email_id, '(RFC822)')
        raw_email = msg_data[0][1]
        msg = email.message_from_bytes(raw_email)
        return msg

    # Function to extract email body
    def extract_email_body(msg):
        body = ""
        if msg.is_multipart():
            for part in msg.walk():
                content_type = part.get_content_type()
                content_disposition = str(part.get("Content-Disposition"))

                if "attachment" not in content_disposition:
                    if content_type == "text/plain":
                        body += part.get_payload(decode=True).decode()
                    elif content_type == "text/html":
                        body += part.get_payload(decode=True).decode()
        else:
            body = msg.get_payload(decode=True).decode()
        return body

    # Function to download PDF attachments and return their paths
    def download_pdf_attachments(msg, download_folder):
        if not os.path.exists(download_folder):
            os.makedirs(download_folder)

        pdf_paths = []
        for part in msg.walk():
            if part.get_content_maintype() == 'multipart':
                continue
            if part.get('Content-Disposition') is None:
                continue
            if part.get_filename() and part.get_filename().endswith('.pdf'):
                filename = part.get_filename()
                filepath = os.path.join(download_folder, filename)
                with open(filepath, 'wb') as f:
                    f.write(part.get_payload(decode=True))
                pdf_paths.append(filepath)

        return pdf_paths

    # Function to save text to a file
    def save_text_to_file(text, filename):
        text = text.lower()
        with open(filename, 'w', encoding='utf-8') as f:
            f.write(text)

    async def get_batch_response(questions, embeddings, vector_stores, semaphore, retries=3):
        async with semaphore:
            for attempt in range(retries):
                try:
                    loop = asyncio.get_event_loop()
                    docs = await loop.run_in_executor(None, vector_stores.similarity_search, questions)
                    chain = get_conversational_chain()
                    response = await loop.run_in_executor(None, chain, {"input_documents": docs, "questions": questions}, True)
                    return response["output_text"]
                except Exception as e:
                    print(f"Attempt {attempt + 1} failed for questions '{questions}' with error: {e}")
                    await asyncio.sleep(2 ** attempt)  # Exponential backoff
            raise Exception(f"Failed to get response for questions '{questions}' after {retries} attempts")

    def generate_question(field_id):
        return f"{field_id}?"

    # Add this function to check if IEC exists in the database
    def check_iec_in_database(iec):
        try:
            connection = get_db_connection()
            cursor = connection.cursor(dictionary=True)

            query = "SELECT * FROM cha WHERE IEC = %s"
            cursor.execute(query, (iec,))
            result = cursor.fetchone()
            connection.commit()

            return result is not None
        except Error as e:
            print(f"Database error: {e}")
            return False
        finally:
            if connection.is_connected():
                cursor.close()
                connection.close()

    async def collect_batch_data(batch, semaphore):
        batch_data = {}
        questions = []
        field_ids = []
        excluded_ids = []  # Initialize excluded_ids list

        for element in batch:
            field_id = element.get_attribute("id")
            if field_id:
                if field_id == "User-Job-Date":
                    current_date = datetime.now().strftime("%d-%m-%Y")
                    batch_data[field_id] = current_date
                elif field_id == "Supporting_Documents_Upload":
                    file_path = r"D:\flask_code\Documents\invoice1r.pdf"
                    batch_data[field_id] = file_path
                else:
                    question = generate_question(field_id)
                    questions.append(question)
                    field_ids.append(field_id)

        # Check if there are any questions to ask
        if questions:
            combined_questions = "\n".join(questions)
            response = await get_batch_response(combined_questions, embeddings, vector_stores, semaphore)

            # Debugging: Print the raw response
            print("Raw response received:")
            print(response)

            # Process the response
            lines = response.split('\n')
            current_field_id = None
            for line in lines:
                line = line.strip()
                if line.startswith('**') and '?**' in line:
                    parts = line.split('?**')
                    if len(parts) == 2:
                        current_field_id = parts[0].strip('*')
                        answer = parts[1].strip()
                        # Store the answer even if it's "n/a"
                        batch_data[current_field_id] = answer

                        # Check if the current field is IEC and update excluded_ids
                        if current_field_id == "IEC" and check_iec_in_database(answer):
                            excluded_ids.extend(["Exporter_Name", "GSTN_ID"])

                elif (line.startswith('*') or line.startswith('**')) and ':**' in line:
                    parts = line.split(':**')
                    if len(parts) == 2:
                        current_field_id = parts[0].strip('* *')
                        answer = parts[1].strip()
                        # Store the answer even if it's "n/a"
                        batch_data[current_field_id] = answer

                        # Check if the current field is IEC and update excluded_ids
                        if current_field_id == "IEC" and check_iec_in_database(answer):
                            excluded_ids.extend(["Exporter_Name", "GSTN_ID"])

                elif current_field_id:
                    # Handle continuation of the previous answer
                    answer = line.strip()
                    if answer:
                        batch_data[current_field_id] += f" {answer}"
                    current_field_id = None

        # Debugging: Print the batch data
        print("Batch data to be filled:")
        print(batch_data)
        print("Excluded IDs:")
        print(excluded_ids)

        return batch_data, excluded_ids

    async def fill_field(element, answer):
        # Debugging: Print the field ID and the answer being filled
        print(f"Filling field {element.get_attribute('id')} with answer: {answer}")
        element.clear()
        element.send_keys(answer)

    async def fill_fields_in_batches(elements, batch_size, delay, semaphore, msgs):
        next_batch_data_task = None
        excluded_ids = []  # Initialize excluded_ids list

        for i in range(0, len(elements), batch_size):
            batch = elements[i:i + batch_size]

            if next_batch_data_task:
                batch_data, new_excluded_ids = await next_batch_data_task
                excluded_ids.extend(new_excluded_ids)
            else:
                batch_data, new_excluded_ids = await collect_batch_data(batch, semaphore)
                excluded_ids.extend(new_excluded_ids)

            # Start processing the next batch in parallel
            if i + batch_size < len(elements):
                next_batch = elements[i + batch_size:i + 2 * batch_size]
                next_batch_data_task = asyncio.create_task(collect_batch_data(next_batch, semaphore))

            tasks = []
            for element in batch:
                field_id = element.get_attribute("id")
                if field_id in batch_data and field_id not in excluded_ids:
                    tasks.append(fill_field(element, batch_data[field_id]))
            await asyncio.gather(*tasks)
            await asyncio.sleep(delay)
        # return render_template("index.html", messages=msgs)

    # Function to extract the first thirty characters from PDF using pdfplumber
    def extract_first_twenty_chars_from_pdf(filename):
        text = ""
        with pdfplumber.open(filename) as pdf:
            for page in pdf.pages:
                text += page.extract_text()
                if len(text) >= 20:
                    break
        return text[:20].lower()  # Return only the first 20 characters

    # Function to check the first few characters of a text file for a specific phrase
    def check_for_phrase_in_file(file_path, phrase, num_chars=100):
        with open(file_path, 'r', encoding='utf-8') as f:
            # Read the first few characters
            content = f.read(num_chars)

        # Check if the phrase exists in the content
        return phrase in content

    # Function to process based on the presence of the phrase
    def process_based_on_phrase(file_path):
        phrase = "new job request"

        if check_for_phrase_in_file(file_path, phrase):
            print("Phrase found. Returning 1...")
            return True
        else:
            print("Phrase not found. Returning 0...")
            return False
    # User credentials
    username = 'vedanshgupta606@gmail.com'
    email_password = os.getenv('EMAIL_PASSWORD')

    # Login to email
    mail = login_to_email(username, email_password)

    # Select the mailbox you want to use (in this case, the inbox)
    mail.select('inbox')

    # Define your search criteria (e.g., FROM, SUBJECT, SINCE)
    search_criteria = '(FROM "gvedansh33@gmail.com")'

    # Search for emails based on criteria
    email_ids = search_emails(mail, search_criteria)
    msgs = []
    
    if email_ids:
        # Print email IDs and choose one
        print("Email IDs matching the criteria:")
        msgs.append("Email IDs matching the criteria: ")
        for i, email_id in enumerate(email_ids):
            print(f"{i + 1}: {email_id.decode('utf-8')}")
            msgs.append(f"{i + 1}: {email_id.decode('utf-8')}")

        # Input: Choose email by ID
        chosen_index = len(email_ids) - 1  # Select the last email in the list
        chosen_id = email_ids[chosen_index]

        # Fetch the email by ID
        msg = fetch_email_by_id(mail, chosen_id)

        # Define download folder
        download_folder = r"Documents/"

        # Download PDF attachments and get their paths
        pdf_paths = download_pdf_attachments(msg, download_folder)

        pdf_docs = []
        if pdf_paths:
            print("\nPDF attachments downloaded to:\n")
            for pdfs in pdf_paths:
                print(pdfs)

                # Extract text from PDF using pdfplumber
                text_pdfplumber = extract_first_twenty_chars_from_pdf(pdfs)
                print("Text extracted from PDF using pdfplumber:")
                print(text_pdfplumber)
                if ("invoice" in text_pdfplumber) or ("packing list" in text_pdfplumber):
                    pdf_docs.append(pdfs)
                print("\n")
                msgs.append(f"Text extracted from PDF {pdfs} using pdfplumber: {text_pdfplumber}")
                
        else:
            print("No PDF attachments found.")
            return jsonify({"status": "No PDF attachments found."})

        # Extract the email body (text written by the sender)
        email_body = extract_email_body(msg)
        email_body_filename = "Documents/email_body.txt"
        save_text_to_file(email_body, email_body_filename)
        print(f"Email body saved to {email_body_filename}")

    else:
        print("No emails matching the criteria.")

    if process_based_on_phrase(email_body_filename):
        # Measure time for making FAISS index
        start_faiss_time = time.time()
        print(f"\n The PDF extracted for processing are:")
        msgs.append("\n The PDF extracted for processing are:")
        for pdf in pdf_docs:
            print(pdf)
            msgs.append(pdf)
        raw_text = get_pdf_text(pdf_docs)
        text_chunks = get_text_chunks(raw_text)
        get_vector_stores(text_chunks)
        end_faiss_time = time.time()
        faiss_duration = end_faiss_time - start_faiss_time
        print(f"Time taken to create FAISS index: {faiss_duration} seconds")
        msgs.append(f"Time taken to create FAISS index: {faiss_duration} seconds")


        # Load the vector stores and embeddings once
        embeddings = GoogleGenerativeAIEmbeddings(model="models/text-embedding-004")
        vector_stores = FAISS.load_local("faiss-index", embeddings, allow_dangerous_deserialization="True")

        # Set up the webdriver for Edge
        edge_options = Options()
        edge_options.use_chromium = True

        driver = webdriver.Edge(options=edge_options)

        try:
            # Open the webpage
            driver.get("https://sb-form.onrender.com/")
            driver.maximize_window()

            # Retrieve all input fields
            all_elements = driver.find_elements(By.TAG_NAME, "input")

            excluded_ids = []

            # Measure time for filling the form
            start_fill_time = time.time()

            # Define a semaphore to limit concurrent requests
            semaphore = asyncio.Semaphore(2)  # Adjust the number of concurrent requests as needed
            
            # Run the async function
            asyncio.run(fill_fields_in_batches(all_elements, batch_size=246, delay=2, semaphore=semaphore, msgs=msgs))

            end_fill_time = time.time()
            fill_duration = end_fill_time - start_fill_time
            # return result.return_response(message=f'Time taken to fill the form: {fill_duration} seconds')
            print(f"Time taken to fill the form: {fill_duration} seconds")
            return render_template("index.html", messages=msgs)

            # time.sleep(10)


        except Exception as e:
            print(f"An error occurred: {e}")
            return jsonify({"status": f"An error occurred: {str(e)}"})

        # finally:
        #     driver.quit()
    else:
        print("\nNo new job to be created")
        return jsonify({"status": "\nNo new job to be created"})

@app.route('/save', methods=['POST'])
def save_user_details():
    user_details = request.get_json()

    print("Received Data:", user_details)  # Debugging line

    if not user_details:
        return jsonify({"status": "error", "message": "Invalid data format"}), 400
    
    try:
        connection = get_db_connection()
        cursor = connection.cursor()

        insert_query = """
        INSERT INTO cha (IEC, Exporter_Name, Adress1, City, State, Pin, Authorized_Dealer_Code, GSTN_Num, GSTN_ID) 
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """
        print("I'm here")
        cursor.execute(insert_query, (user_details['IEC'], user_details['Exporter_Name'], user_details['Adress1'], user_details['City'], user_details['State'], user_details['Pin'], user_details['Authorized_Dealer_Code'], user_details['GSTN_Num'], user_details['GSTN_ID']))

        connection.commit()
        
        return jsonify({"status": "success", "message": "User details saved successfully"}), 200

    except Error as e:
        return jsonify({"status": "error", "message": str(e)}), 500

    # finally:
    #     if connection.is_connected():
    #         cursor.close()
    #         connection.close()

@app.route('/fetch', methods=['GET'])
def fetch_user_details():
    identifier = request.args.get('identifier')

    try:
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)

        # Assuming 'identifier' is email
        fetch_query = "SELECT * FROM cha WHERE IEC = %s"
        cursor.execute(fetch_query, (identifier,))
        user_details = cursor.fetchone()

        if user_details:
            return jsonify({"status": "success", "data": user_details}), 200
        else:
            return jsonify({"status": "error", "message": "User not found"}), 404

    except Error as e:
        return jsonify({"status": "error", "message": str(e)}), 500

    # finally:
    #     if connection.is_connected():
    #         cursor.close()
    #         connection.close()


# if __name__ == '__main__':
#     app.run(debug=True)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)