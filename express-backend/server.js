import axios from 'axios';
import aiRoutes from "./routes/ai.js";

const express = require('express');
const cors = require('cors');
const uploadRoute = require('./routes/uploadRoute');

const app = express();
app.use(cors());
app.use(express.json());
app.use(express.urlencoded({ extended: true }));

// mount upload routes at root so endpoints match frontend (/files, /upload, /delete_upload/...)
app.use('/', uploadRoute);

app.use("/api/ai", aiRoutes);

// Health check endpoint (used by frontend checkServer)
app.get('/health', (req, res) => {
  res.status(200).json({ status: 'ok' });
});

const PORT = process.env.PORT || 3000;
app.listen(PORT, () => console.log(`Server listening on port ${PORT}`));