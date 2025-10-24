const { MongoClient ,ObjectId } = require('mongodb');
const pdf = require('pdf-parse');
const XLSX = require('xlsx');
const fs = require('fs');

const url = 'mongodb://localhost:27017/';
const client = new MongoClient(url, {
  useUnifiedTopology: true,
  useNewUrlParser: true
});

async function xlsxFormat(fileBuffer) {

  try {

    const workbook = XLSX.read(fileBuffer, { type: 'buffer' });
    const sheetName = workbook.SheetNames[0];
    const sheet = workbook.Sheets[sheetName];
    return XLSX.utils.sheet_to_json(sheet);

  } catch (error) {
    console.log('Error converting xlsx', error.message);
  }

}

async function pdfFormat(buffer) {

  try {

    return await pdf(buffer);

  } catch (error) {
    console.log('Error converting pdf', error.message);
  }
}

async function retrieve_file(id) {
  try {

    await client.connect();

    const database = client.db('file');
    const collection = database.collection('files');

    const fileDocument = await collection.findOne({ _id: new ObjectId(id)});
    const fileBuffer = fileDocument.file.buffer;

    if (
      fileDocument.file_name.endsWith('.xlsx') ||
      fileDocument.fileType.includes('spreadsheet')
    ) {
      const xlsxData = await xlsxFormat(fileBuffer);
    } else if (fileDocument.fileType === 'application/pdf') {
      const pdfData = await pdfFormat(fileBuffer);
    } else {
      console.log('Unsupported file type:', fileDocument.fileType);
    }

    fs.writeFileSync(fileDocument.file_name, fileBuffer);

  } catch (error) {
    console.log('Error retrieving file:',error.message);
  } finally {
    await client.close();
  }
}