const mongoose = require('mongoose');
const crypto = require('crypto');
const { userSchema, fileSchema } = require('./../components/constructor');
const url_1 = "mongodb://localhost:27017/accounts";
const url_2 = "mongodb://localhost:27017/file";

const AccountsConnection = mongoose.createConnection(url_1, {
  useNewUrlParser: true,
  useUnifiedTopology: true
});
const FileConnection = mongoose.createConnection(url_2, {
  useNewUrlParser: true,
  useUnifiedTopology: true
});

const User = AccountsConnection.model('User', userSchema);
const File = FileConnection.model('File', fileSchema);

async function connection() {

  try {

    await Promise.all([
      AccountsConnection.asPromise(),
      FileConnection.asPromise(),
    ]);

    console.log("Connection established.");

  } catch (error) {
    console.error("Connection Failed", error.message);
  }

}

async function register(userData) {
  try {

    const user = new User(userData);
    const savedUser = await user.save();
    console.log("User registered.");

    return savedUser;

  } catch (error) {

    if (error.code === 11000) {

      console.error(`Registration Error.`);
      return { status: 409, message: "There was an error with your registration" };

    }

    console.error("Error registering user:", error.message);
    return { status: 500, message: "Internal Server Error" };
  }
}

async function upload(userFile) {
  try {

    const fileHash = crypto.createHash('sha256').update(userFile.file).digest('hex');
    const existingfile = await File.findOne({ fileHash });

    if (existingfile) {
      console.error(" Duplicate file content ");
      return { status: 409, message: "Duplicate file content" };
    }

    const file = new File({ ...userFile, fileHash });
    const saveFile = await file.save();
    console.log("File uploaded successfully.");

    return saveFile;

  } catch (error) {

    if (error.code === 11000) {

      console.error('Duplicate file')
    }

    console.error("Error uploading file:", error.message);
  }

}

module.exports = { connection, register, upload };