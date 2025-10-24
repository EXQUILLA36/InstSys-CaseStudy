// studentDatabase.js
const { MongoClient } = require('mongodb');
const xlsx = require('xlsx');
const fs = require('fs').promises;
const path = require('path');

// Field Status Enum
const FieldStatus = {
  COMPLETE: 'complete',
  WAITING: 'waiting',
  MISSING: 'missing'
};

const MediaDefaults = {
  IMAGE: {
    data: 'default_profile',
    filename: 'default_profile.jpg',
    path: '/images/default_profile.jpg'
  },
  AUDIO: {
    data: null,  // No default audio
    filename: null,
    path: null
  }
};

class StudentDatabase {
  constructor(connectionString = null, databaseName = 'school_system') {
  this.connectionString = connectionString || 'mongodb://localhost:27017/';
  this.databaseName = databaseName;
  this.client = null;
  this.db = null;
  
  // Department collections (NEW!)
  this.collections = {
    ccs: null,
    chtm: null,
    cba: null,
    cte: null,
    unknown: null
  };
  
  this.pendingMedia = null;
  }

  async connect() {
  try {
    this.client = new MongoClient(this.connectionString, {
      serverSelectionTimeoutMS: 5000
    });

    await this.client.connect();
    await this.client.db().admin().ping();
    console.log('✅ Connected to MongoDB successfully');

    this.db = this.client.db(this.databaseName);
    
    // Initialize department collections
    this.collections.ccs = this.db.collection('students_ccs');
    this.collections.chtm = this.db.collection('students_chtm');
    this.collections.cba = this.db.collection('students_cba');
    this.collections.cte = this.db.collection('students_cte');
    this.collections.unknown = this.db.collection('students_unknown');
    
    this.pendingMedia = this.db.collection('pending_media');

    await this._createIndexes();
  } catch (error) {
    console.error(`❌ MongoDB Connection Error: ${error.message}`);
    console.log('\n💡 Troubleshooting:');
    console.log('   1. Make sure MongoDB is running:');
    console.log('      - Windows: net start MongoDB');
    console.log('      - Or run: mongod --dbpath C:\\data\\db');
    console.log('   2. Or use MongoDB Atlas (cloud): https://www.mongodb.com/cloud/atlas');
    throw error;
  }
  }

  async _createIndexes() {
  // Create indexes for each department collection
  const indexConfig = [
    { student_id: 1 },
    { surname: 1 },
    { first_name: 1 },
    { course: 1 },
    { section: 1 },
    { year: 1 },
    { course: 1, year: 1, section: 1 } // Compound index
  ];

  for (const [dept, collection] of Object.entries(this.collections)) {
    // Create unique index for student_id
    await collection.createIndex({ student_id: 1 }, { unique: true });
    
    // Create other indexes
    for (const index of indexConfig.slice(1)) {
      await collection.createIndex(index);
    }
  }

  // Pending media indexes
  await this.pendingMedia.createIndex({ student_id: 1 });
  await this.pendingMedia.createIndex({ department: 1 });
  await this.pendingMedia.createIndex({ status: 1 });
}


_getCollectionByDepartment(department) {
  const dept = (department || 'UNKNOWN').toLowerCase();
  return this.collections[dept] || this.collections.unknown;
}


  async createStudentRecord(data, source = 'file_extraction') {
  try {
    const studentDoc = {
      student_id: data.student_id || '',
      surname: data.surname || '',
      first_name: data.first_name || '',
      full_name: data.full_name || '',
      course: data.course || '',
      section: data.section || '',
      year: data.year || '',
      contact_number: data.contact_number || '',
      guardian_name: data.guardian_name || '',
      guardian_contact: data.guardian_contact || '',
      department: data.department || 'UNKNOWN',
      descriptor: data.descriptor || null,

      image: {
        data: data.image_data || null,
        filename: data.image_filename || null,
        status: source === 'file_extraction' 
          ? FieldStatus.WAITING 
          : (data.image_data ? FieldStatus.COMPLETE : FieldStatus.WAITING)
      },
      audio: {
        data: data.audio_data || null,
        filename: data.audio_filename || null,
        status: source === 'file_extraction'
          ? FieldStatus.WAITING
          : (data.audio_data ? FieldStatus.COMPLETE : FieldStatus.WAITING)
      },

      field_status: this._determineFieldStatus(data, source),
      source: source,
      created_at: new Date(),
      updated_at: new Date(),
      completion_percentage: this._calculateCompletion(data, source)
    };

    // Get the appropriate collection based on department
    const collection = this._getCollectionByDepartment(studentDoc.department);

    const result = await collection.updateOne(
      { student_id: studentDoc.student_id },
      { $set: studentDoc },
      { upsert: true }
    );

    if (studentDoc.image.status === FieldStatus.WAITING || 
        studentDoc.audio.status === FieldStatus.WAITING) {
      await this._addToPendingMedia(studentDoc);
    }

    console.log(`✅ Student record created/updated in ${studentDoc.department}: ${studentDoc.student_id}`);
    return studentDoc.student_id;

  } catch (error) {
    console.error(`❌ Error creating student record: ${error.message}`);
    return null;
  }
}

  _determineFieldStatus(data, source) {
    const fieldStatus = {};
    const textFields = ['student_id', 'surname', 'first_name', 'course', 'section', 'year'];

    textFields.forEach(field => {
      const value = data[field] || '';
      if (source === 'manual_input') {
        fieldStatus[field] = value ? FieldStatus.COMPLETE : FieldStatus.WAITING;
      } else {
        fieldStatus[field] = value ? FieldStatus.COMPLETE : FieldStatus.MISSING;
      }
    });

    fieldStatus.image = FieldStatus.WAITING;
    fieldStatus.audio = FieldStatus.WAITING;

    return fieldStatus;
  }

  _calculateCompletion(data, source) {
    const totalFields = 9;
    let completed = 0;

    const textFields = ['student_id', 'surname', 'first_name', 'course', 'section', 'year'];
  textFields.forEach(field => {
    if (data[field]) completed++;
  });

  if (data.image_data) completed++;
  if (data.audio_data) completed++;
  // Add descriptor check
  if (data.descriptor) completed++;  // ← ADD THIS LINE

  return (completed / totalFields) * 100;
}

  async _addToPendingMedia(studentDoc) {
    const pendingDoc = {
      student_id: studentDoc.student_id,
      full_name: studentDoc.full_name,
      course: studentDoc.course,
      section: studentDoc.section,
      year: studentDoc.year,
      waiting_for: {
        image: studentDoc.image.status === FieldStatus.WAITING,
        audio: studentDoc.audio.status === FieldStatus.WAITING
      },
      added_at: new Date()
    };

    await this.pendingMedia.updateOne(
      { student_id: studentDoc.student_id },
      { $set: pendingDoc },
      { upsert: true }
    );
  }

  async updateMedia(studentId, mediaType, mediaData, filename, department) {
  try {
    // Get the appropriate collection
    const collection = this._getCollectionByDepartment(department);
    
    const updateData = {
      [`${mediaType}.data`]: mediaData,
      [`${mediaType}.filename`]: filename,
      [`${mediaType}.status`]: FieldStatus.COMPLETE,
      [`field_status.${mediaType}`]: FieldStatus.COMPLETE,
      updated_at: new Date()
    };

    const result = await collection.updateOne(
      { student_id: studentId },
      { $set: updateData }
    );

    if (result.modifiedCount > 0) {
      await this._updateCompletionPercentage(studentId, department);
      await this._checkPendingMediaComplete(studentId, department);
      console.log(`✅ Updated ${mediaType} for student ${studentId}`);
      return true;
    } else {
      console.log(`⚠️ Student ${studentId} not found`);
      return false;
    }

  } catch (error) {
    console.error(`❌ Error updating media: ${error.message}`);
    return false;
  }
}

  async _updateCompletionPercentage(studentId, department) {
  const collection = this._getCollectionByDepartment(department);
  const student = await collection.findOne({ student_id: studentId });
  if (!student) return;

  const totalFields = 9;
  let completed = 0;

  const textFields = ['student_id', 'surname', 'first_name', 'course', 'section', 'year'];
  textFields.forEach(field => {
    if (student[field]) completed++;
  });

  if (student.image?.status === FieldStatus.COMPLETE) completed++;
  if (student.audio?.status === FieldStatus.COMPLETE) completed++;
  if (student.descriptor) completed++;

  const completion = (completed / totalFields) * 100;

  await collection.updateOne(
    { student_id: studentId },
    { $set: { completion_percentage: completion } }
  );
}


async updateDescriptor(studentId, descriptor, department) {
  try {
    const collection = this._getCollectionByDepartment(department);
    
    const result = await collection.updateOne(
      { student_id: studentId },
      { 
        $set: { 
          descriptor: descriptor,
          updated_at: new Date()
        } 
      }
    );

    if (result.modifiedCount > 0) {
      await this._updateCompletionPercentage(studentId, department);
      console.log(`✅ Updated descriptor for student ${studentId}`);
      return true;
    } else {
      console.log(`⚠️ Student ${studentId} not found in ${department}`);
      return false;
    }

  } catch (error) {
    console.error(`❌ Error updating descriptor: ${error.message}`);
    return false;
  }
}

  async _checkPendingMediaComplete(studentId, department) {
  const collection = this._getCollectionByDepartment(department);
  const student = await collection.findOne({ student_id: studentId });
  if (!student) return;

  const imageComplete = student.image?.status === FieldStatus.COMPLETE;
  const audioComplete = student.audio?.status === FieldStatus.COMPLETE;

  if (imageComplete && audioComplete) {
    await this.pendingMedia.deleteOne({ student_id: studentId });
    console.log(`🎉 Student ${studentId} completed all media requirements`);
  }
}

  async getPendingMediaStudents() {
    return await this.pendingMedia.find({}).toArray();
  }

  async searchStudents(query = null, filters = null) {
  const searchFilter = {};

  if (query) {
    searchFilter.$or = [
      { surname: { $regex: query, $options: 'i' } },
      { first_name: { $regex: query, $options: 'i' } },
      { full_name: { $regex: query, $options: 'i' } },
      { student_id: { $regex: query, $options: 'i' } }
    ];
  }

  if (filters) {
    Object.keys(filters).forEach(key => {
      if (key === 'year') {
        searchFilter[key] = String(filters[key]);
      } else if (key !== 'department') {
        searchFilter[key] = filters[key];
      }
    });
  }

  // If department filter is specified, search only that collection
  if (filters?.department) {
    const collection = this._getCollectionByDepartment(filters.department);
    return await collection.find(searchFilter).toArray();
  }

  // Otherwise, search all collections
  const results = [];
  for (const [dept, collection] of Object.entries(this.collections)) {
    const deptResults = await collection.find(searchFilter).toArray();
    results.push(...deptResults);
  }

  return results;
}

  async getStudentById(studentId, department = null) {
  // If department is specified, search only that collection
  if (department) {
    const collection = this._getCollectionByDepartment(department);
    return await collection.findOne({ student_id: studentId });
  }

  

  // Otherwise, search all collections
  for (const [dept, collection] of Object.entries(this.collections)) {
    const student = await collection.findOne({ student_id: studentId });
    if (student) return student;
  }

  return null;
}

  getStudentDisplay(student) {
  if (!student) return null;

  // Clone the student object to avoid modifying the original
  const displayStudent = JSON.parse(JSON.stringify(student));

  // Replace null/waiting image with default
  const imageIsEmpty = !displayStudent.image?.data || 
                       displayStudent.image?.data === null ||
                       displayStudent.image?.status === FieldStatus.WAITING;

  if (imageIsEmpty) {
    displayStudent.image = {
      data: MediaDefaults.IMAGE.data,
      filename: MediaDefaults.IMAGE.filename,
      display_path: MediaDefaults.IMAGE.path,
      status: displayStudent.image?.status || FieldStatus.WAITING,
      is_default: true
    };
  } else {
    displayStudent.image.is_default = false;
    displayStudent.image.display_path = `/images/${displayStudent.image.filename}`;
  }

  // Handle audio
  const audioIsEmpty = !displayStudent.audio?.data || 
                       displayStudent.audio?.data === null ||
                       displayStudent.audio?.status === FieldStatus.WAITING;

  if (audioIsEmpty) {
    displayStudent.audio = {
      data: MediaDefaults.AUDIO.data,
      filename: MediaDefaults.AUDIO.filename,
      display_path: MediaDefaults.AUDIO.path,
      status: displayStudent.audio?.status || FieldStatus.WAITING,
      is_default: true
    };
  } else {
    displayStudent.audio.is_default = false;
    displayStudent.audio.display_path = `/audio/${displayStudent.audio.filename}`;
  }

  return displayStudent;
}

getStudentsDisplay(students) {
  if (!students || !Array.isArray(students)) return [];
  return students.map(student => this.getStudentDisplay(student));
}


// Get student by ID with display defaults
async getStudentByIdWithDefaults(studentId, department = null) {
  const student = await this.getStudentById(studentId, department);
  return this.getStudentDisplay(student);
}

// Get multiple students with display defaults
async getStudentsWithDefaults(query = null, filters = null) {
  const students = await this.searchStudents(query, filters);
  return students.map(student => this.getStudentDisplay(student));
}


  async getStatistics() {
  let totalStudents = 0;
  const byDepartment = {};

  // Get counts from each department collection
  for (const [dept, collection] of Object.entries(this.collections)) {
    const count = await collection.countDocuments({});
    totalStudents += count;
    if (count > 0) {
      byDepartment[dept.toUpperCase()] = count;
    }
  }

  const pendingMedia = await this.pendingMedia.countDocuments({});

  // Calculate average completion across all departments
  let totalCompletion = 0;
  let studentCount = 0;

  for (const collection of Object.values(this.collections)) {
    const avgResult = await collection.aggregate([
      {
        $group: {
          _id: null,
          avg_completion: { $avg: '$completion_percentage' },
          count: { $sum: 1 }
        }
      }
    ]).toArray();

    if (avgResult.length > 0) {
      totalCompletion += avgResult[0].avg_completion * avgResult[0].count;
      studentCount += avgResult[0].count;
    }
  }

  const avgCompletion = studentCount > 0 ? totalCompletion / studentCount : 0;

  return {
    total_students: totalStudents,
    pending_media: pendingMedia,
    average_completion: Math.round(avgCompletion * 100) / 100,
    by_department: byDepartment
  };
}

  async viewAllStudents(limit = 50, department = null) {
  if (department) {
    const collection = this._getCollectionByDepartment(department);
    return await collection.find({}).limit(limit).toArray();
  }

  // Get from all departments
  const results = [];
  for (const collection of Object.values(this.collections)) {
    const students = await collection.find({}).limit(limit).toArray();
    results.push(...students);
    if (results.length >= limit) break;
  }

  return results.slice(0, limit);
}

  async viewStudentDetails(studentId) {
    return await this.students.findOne({ student_id: studentId });
  }

  async exportToDict() {
    return {
      students: await this.students.find({}).toArray(),
      pending_media: await this.pendingMedia.find({}).toArray()
    };
  }

  async clearAllData() {
  // Clear all department collections
  for (const collection of Object.values(this.collections)) {
    await collection.deleteMany({});
  }
  await this.pendingMedia.deleteMany({});
  console.log('🗑️ All data cleared from all department collections');
}

async getStudentsByDepartment(department) {
  const collection = this._getCollectionByDepartment(department);
  return await collection.find({}).sort({ 
    course: 1, 
    year: 1, 
    section: 1,
    surname: 1 
  }).toArray();
}

async getDepartmentStatistics(department) {
  const collection = this._getCollectionByDepartment(department);
  
  const totalStudents = await collection.countDocuments({});
  
  const avgResult = await collection.aggregate([
    {
      $group: {
        _id: null,
        avg_completion: { $avg: '$completion_percentage' }
      }
    }
  ]).toArray();

  const avgCompletion = avgResult.length > 0 ? avgResult[0].avg_completion : 0;

  // By course
  const byCourse = await collection.aggregate([
    {
      $group: {
        _id: { course: '$course', year: '$year', section: '$section' },
        count: { $sum: 1 }
      }
    },
    {
      $sort: { '_id.course': 1, '_id.year': 1, '_id.section': 1 }
    }
  ]).toArray();

  return {
    department: department.toUpperCase(),
    total_students: totalStudents,
    average_completion: Math.round(avgCompletion * 100) / 100,
    by_course: byCourse
  };
}

  async close() {
    if (this.client) {
      await this.client.close();
    }
  }
}

class StudentDataExtractor {
  static async processExcel(filePath, db) {
    try {
      const workbook = xlsx.readFile(filePath);
      const sheetName = workbook.SheetNames[0];
      const worksheet = workbook.Sheets[sheetName];
      const data = xlsx.utils.sheet_to_json(worksheet);

      const columnMapping = {
        'student id': 'student_id',
        'id no': 'student_id',
        'id': 'student_id',
        'full name': 'full_name',
        'name': 'full_name',
        'surname': 'surname',
        'first name': 'first_name',
        'year': 'year',
        'course': 'course',
        'section': 'section',
        'contact number': 'contact_number',
        'guardian name': 'guardian_name',
        'guardian contact': 'guardian_contact'
      };

      let processedCount = 0;

      for (const row of data) {
        const studentData = {};

        const normalizedRow = {};
        Object.keys(row).forEach(key => {
          normalizedRow[key.toLowerCase().trim()] = row[key];
        });

        Object.keys(columnMapping).forEach(colHeader => {
          const dataKey = columnMapping[colHeader];
          if (normalizedRow[colHeader] !== undefined && normalizedRow[colHeader] !== null) {
            const rawValue = String(normalizedRow[colHeader]).trim();
            if (rawValue && !['nan', '', 'null'].includes(rawValue.toLowerCase())) {
              studentData[dataKey] = this.cleanValue(rawValue, dataKey);
            }
          }
        });

        if (studentData.course) {
          studentData.department = this.detectDepartment(studentData.course);
        }

        if (!studentData.full_name && studentData.surname && studentData.first_name) {
          studentData.full_name = `${studentData.surname}, ${studentData.first_name}`;
        }

        if (studentData.student_id || studentData.full_name) {
          const result = await db.createStudentRecord(studentData, 'file_extraction');
          if (result) processedCount++;
        }
      }

      console.log(`📊 Processed ${processedCount} students from Excel`);
      return processedCount > 0;

    } catch (error) {
      console.error(`❌ Error processing Excel: ${error.message}`);
      return false;
    }
  }

  static cleanValue(value, fieldType) {
    if (!value) return null;

    value = value.trim();

    if (fieldType === 'student_id') {
      return value.toUpperCase().replace(/[^A-Z0-9-]/g, '');
    } else if (['contact_number', 'guardian_contact'].includes(fieldType)) {
      const cleaned = value.replace(/[^\d+]/g, '');
      return (cleaned.length >= 7 && cleaned.length <= 15) ? cleaned : null;
    } else if (['full_name', 'guardian_name', 'surname', 'first_name'].includes(fieldType)) {
      return value.replace(/[^A-Za-z\s.,-]/g, '').split(' ')
        .map(word => word.charAt(0).toUpperCase() + word.slice(1).toLowerCase())
        .join(' ');
    } else if (fieldType === 'year') {
      const yearMatch = value.match(/([1-4])/);
      return yearMatch ? yearMatch[1] : null;
    } else if (['course', 'section'].includes(fieldType)) {
      return value.toUpperCase().replace(/[^A-Z0-9]/g, '');
    }

    return value;
  }

  static detectDepartment(courseCode) {
    if (!courseCode) return 'UNKNOWN';

    const courseUpper = String(courseCode).toUpperCase().trim();

    const knownCourses = {
      'CCS': ['BSCS', 'BSIT'],
      'CHTM': ['BSHM', 'BSTM'],
      'CBA': ['BSBA', 'BSOA'],
      'CTE': ['BECED', 'BTLE']
    };

    for (const [dept, courses] of Object.entries(knownCourses)) {
      if (courses.includes(courseUpper)) {
        return dept;
      }
    }

    return 'UNKNOWN';
  }
}

module.exports = { StudentDatabase, StudentDataExtractor, FieldStatus, MediaDefaults };