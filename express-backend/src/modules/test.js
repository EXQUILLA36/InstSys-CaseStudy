const { connection, register, upload } = require('./connection');
const fs = require('fs');

const fileBuffer = fs.readFileSync('./BSCS_2ndYear_A.xlsx');

const userFile = {
  file_name: 'BSCS_2ndYear_A.xlsx',   // or test.xlsx
  fileType: 'application/pdf', // or application/vnd.openxmlformats-officedocument.spreadsheetml.sheet
  file: fileBuffer
};

(async () => {
  await connection();

  const userFile = {
    file_name: 'BSCS_2ndYear_A.xlsx',
    fileType: 'application/pdf',
    file: fileBuffer
  };

  const result = await upload(userFile);
  console.log("File upload result:", result);

  process.exit(0);
})();
