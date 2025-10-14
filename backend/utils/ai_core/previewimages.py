import json, os, html

# === Resolve Paths ===
base_path = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
json_path = os.path.join(base_path, "latest_response_data_with_images.json")
html_path = os.path.join(base_path, "image_preview.html")

# === Load the latest AI response with image map ===
with open(json_path, "r", encoding="utf-8") as f:
    data = json.load(f)

ai_response = data.get("ai_response", "")
image_map = data.get("image_map", {})
by_id = image_map.get("by_id", {})
by_name = image_map.get("by_name", {})

# === Start building the HTML ===
html_parts = [
    "<html><head>",
    "<meta charset='utf-8'/>",
    "<title>AI Response + Image Preview</title>",
    """
    <style>
        body { font-family: Arial, sans-serif; margin: 30px; background: #fafafa; color: #222; }
        h1, h2 { font-family: 'Segoe UI', sans-serif; color: #333; }
        .response-box {
            background: #fff; border: 1px solid #ddd; padding: 20px;
            border-radius: 8px; margin-bottom: 30px; box-shadow: 0 2px 6px rgba(0,0,0,0.05);
            white-space: pre-wrap;
        }
        .image-section { margin-bottom: 40px; }
        .image-grid {
            display: flex; flex-wrap: wrap; gap: 15px;
        }
        .img-card {
            text-align: center; background: #fff; padding: 10px;
            border-radius: 10px; box-shadow: 0 2px 4px rgba(0,0,0,0.1);
            width: 150px;
        }
        .img-card img {
            width: 130px; height: 130px; object-fit: cover; border-radius: 8px; border: 1px solid #ccc;
        }
        .img-card small { display: block; margin-top: 8px; font-size: 0.9em; color: #555; }
    </style>
    """,
    "</head><body>"
]

# === Add AI Response Section ===
html_parts.append("<h1>AI Response: </h1>")
html_parts.append(f"<div class='response-box'>{html.escape(ai_response)}</div>")

# === Add Image Previews ===
def add_image_section(title, img_dict):
    if not img_dict:
        return
    html_parts.append(f"<div class='image-section'><h2>{title}</h2><div class='image-grid'>")
    for key, base64_data in img_dict.items():
        html_parts.append(f"""
        <div class="img-card">
            <img src="data:image/jpeg;base64,{base64_data}" alt="{key}" />
            <small>{html.escape(key)}</small>
        </div>
        """)
    html_parts.append("</div></div>")

add_image_section("This is who she is: ", by_id)
add_image_section("By Name (Without ID Match)", by_name)

html_parts.append("</body></html>")

# === Save HTML ===
with open(html_path, "w", encoding="utf-8") as f:
    f.write("\n".join(html_parts))

print(f"✅ AI response + image preview generated successfully!\n{html_path}")
