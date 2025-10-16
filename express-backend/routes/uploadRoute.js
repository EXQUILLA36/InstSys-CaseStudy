const express = require('express');
const router = express.Router();
const multer = require('multer');
const fs = require('fs');
const path = require('path');

const UPLOAD_ROOT = path.join(__dirname, '..', 'data-management', 'uploaded_files');
const ALLOWED_FOLDERS = ['faculty', 'students', 'admin'];

// ensure folders exist
for (const f of ALLOWED_FOLDERS) {
  const d = path.join(UPLOAD_ROOT, f);
  if (!fs.existsSync(d)) fs.mkdirSync(d, { recursive: true });
}

// use memory storage so we can check duplicates before writing
const upload = multer({ storage: multer.memoryStorage() });

// List files: GET /files
router.get('/files', (req, res) => {
  try {
    const files = {};
    for (const f of ALLOWED_FOLDERS) {
      const folderPath = path.join(UPLOAD_ROOT, f);
      files[f] = fs.readdirSync(folderPath).filter(fn => fs.statSync(path.join(folderPath, fn)).isFile());
    }
    return res.json({ files });
  } catch (err) {
    console.error(err);
    return res.status(500).json({ error: 'Failed to list files' });
  }
});

// Upload: POST /upload
// form fields: file (file), folder (faculty|students|admin), overwrite (optional 'true')
router.post('/upload', upload.single('file'), (req, res) => {
  try {
    if (!req.file) return res.status(400).json({ error: 'No file uploaded' });
    const folder = (req.body.folder || '').toLowerCase();
    if (!ALLOWED_FOLDERS.includes(folder)) return res.status(400).json({ error: 'Invalid folder' });

    const filename = path.basename(req.file.originalname); // sanitise
    const destPath = path.join(UPLOAD_ROOT, folder, filename);

    if (fs.existsSync(destPath) && req.body.overwrite !== 'true') {
      // duplicate found
      return res.status(409).json({
        duplicate: true,
        message: `File "${filename}" already exists in ${folder}. Overwrite?`,
      });
    }

    // write file
    fs.writeFileSync(destPath, req.file.buffer);
    return res.json({ success: true, message: 'Upload complete' });
  } catch (err) {
    console.error(err);
    return res.status(500).json({ error: 'Upload failed' });
  }
});

// Delete: DELETE /delete_upload/:category/:filename
router.delete('/delete_upload/:category/:filename', (req, res) => {
  try {
    const category = (req.params.category || '').toLowerCase();
    if (!ALLOWED_FOLDERS.includes(category)) return res.status(400).json({ error: 'Invalid category' });

    const filename = decodeURIComponent(req.params.filename);
    const safeName = path.basename(filename);
    const target = path.join(UPLOAD_ROOT, category, safeName);

    if (!fs.existsSync(target)) return res.status(404).json({ error: 'File not found' });

    fs.unlinkSync(target);
    return res.json({ success: true });
  } catch (err) {
    console.error(err);
    return res.status(500).json({ error: 'Delete failed' });
  }
});

module.exports = router;