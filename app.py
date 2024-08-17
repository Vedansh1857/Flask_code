import os
from flask import Flask, request, jsonify
import mysql.connector
from mysql.connector import Error
from flask_cors import CORS

app = Flask(__name__)

CORS(app)

def get_db_connection():
    return mysql.connector.connect(
        host='cheetahdb.cx8yyoqogq59.us-east-1.rds.amazonaws.com',  # e.g., 'RDS Endpoint'
        database='cheetah',  # e.g., 'user_db'
        user='CheetahAI_DB',  # e.g., 'root'
        password=os.getenv('password'),  # your MySQL password
        autocommit=True
    )

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

    finally:
        if connection.is_connected():
            cursor.close()
            connection.close()


if __name__ == '__main__':
    app.run(debug=True)
