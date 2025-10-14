from pymongo import MongoClient
import json, re, os

# 🔹 Connect to MongoDB
client = MongoClient("mongodb://localhost:27017/")
db = client["school_system"]  # adjust your database name if needed


def build_image_map_from_mongo(ai_response_data):
    """
    Searches the MongoDB database for Base64 image data
    matching names and PDM IDs found in the AI response.
    If both name and ID resolve to the same person, only keep the ID entry.
    """
    image_map = {"by_id": {}, "by_name": {}}
    structured_data = ai_response_data.get("structured_data", [])
    content = " ".join([item.get("content", "") for item in structured_data])

    # Extract IDs like PDM-2025-000123
    ids = re.findall(r"(PDM-\d{4}-\d{6})", content)
    # Extract names like "Lastname, Firstname"
    names = re.findall(r"([A-Z][a-z]+,\s[A-Z][a-z]+)", content)

    all_collections = db.list_collection_names()

    def find_image_in_db(filter_query):
        """Search all collections for a document that contains image.data."""
        for coll_name in all_collections:
            coll = db[coll_name]
            record = coll.find_one(filter_query, {"image": 1, "student_id": 1, "full_name": 1})
            if record and record.get("image", {}).get("data"):
                return record
        return None

    # 🔹 Map by PDM ID
    for pid in ids:
        record = find_image_in_db({"student_id": pid})
        if record and record["image"]["data"]:
            image_map["by_id"][pid] = record["image"]["data"]

    # 🔹 Map by Name (only if ID was not already matched)
    for name in names:
        # Skip if same student already handled via ID
        record = find_image_in_db({"full_name": name})
        if not record or not record.get("image", {}).get("data"):
            continue

        student_id = record.get("student_id")
        # Only include in by_name if this student has no entry in by_id
        if not student_id or student_id not in image_map["by_id"]:
            image_map["by_name"][name] = record["image"]["data"]

    return image_map


if __name__ == "__main__":
    base_path = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    input_path = os.path.join(base_path, "latest_response_data.json")
    output_path = os.path.join(base_path, "latest_response_data_with_images.json")

    with open(input_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    image_map = build_image_map_from_mongo(data)
    data["image_map"] = image_map

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    print(f"✅ Image mapping complete! Saved to:\n{output_path}")
