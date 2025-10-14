# prepare_mongo_data.py
import os
from pymongo import MongoClient

# --- CONFIGURATION ---
MONGO_URI = "mongodb://localhost:27017/" # e.g., "mongodb://localhost:27017/"
DB_NAME = "school_system"
COLLECTION_NAME = "students"

def generate_content_field():
    """
    Adds a 'content' field to each document for AI consumption.
    This mimics the structure the AI previously expected from ChromaDB.
    """
    client = MongoClient(MONGO_URI)
    db = client[DB_NAME]
    collection = db[COLLECTION_NAME]

    # Find all documents that do not have the 'content' field yet
    docs_to_process = list(collection.find({"content": {"$exists": False}}))

    if not docs_to_process:
        print(f"✅ All documents in '{COLLECTION_NAME}' already have a 'content' field.")
        return

    print(f"🔥 Found {len(docs_to_process)} documents in '{COLLECTION_NAME}' to update...")

    from pymongo import UpdateOne
    operations = []

    for doc in docs_to_process:
        # This provides a consistent text representation for the AI
        name = doc.get("full_name", "N/A")
        stud_id = doc.get("student_id", "N/A")
        course = doc.get("course", "N/A")
        year = doc.get("year", "N/A")
        section = doc.get("section", "N/A")
        department = doc.get("department", "N/A")

        content_string = (
            f"Student profile for {name}. "
            f"ID: {stud_id}. "
            f"Program: {course}, Year: {year}, Section: {section}. "
            f"Department: {department}."
        )

        operations.append(
            UpdateOne({"_id": doc["_id"]}, {"$set": {"content": content_string}})
        )

    if operations:
        collection.bulk_write(operations)
        print(f"✅ Successfully added a 'content' field to {len(operations)} documents.")
    
    client.close()

if __name__ == "__main__":
    print("Starting data preparation for AI compatibility...")
    generate_content_field()