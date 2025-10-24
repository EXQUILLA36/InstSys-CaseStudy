// runSystem.js
const readline = require('readline');
const path = require('path');
const fs = require('fs').promises;
const { StudentDatabase, StudentDataExtractor } = require('./main');

class SchoolInformationSystem {
  constructor(connectionString = null) {
    this.db = new StudentDatabase(connectionString);
    
    // Paths relative to utils folder
    this.basePath = path.join(__dirname, 'uploaded_files');
    this.studentExcelFolder = path.join(this.basePath, 'student_list_excel');
    this.processedFolder = path.join(this.basePath, 'processed');
    
    // Create readline interface for user input
    this.rl = readline.createInterface({
      input: process.stdin,
      output: process.stdout
    });
    
    console.log(`📁 Looking for Excel files in: ${this.studentExcelFolder}`);
  }

  // Helper function to prompt user for input
  prompt(question) {
    return new Promise((resolve) => {
      this.rl.question(question, (answer) => {
        resolve(answer);
      });
    });
  }

  async clearAllData() {
    try {
      const confirm = await this.prompt('⚠️  Clear ALL student data from MongoDB? (yes/no): ');
      
      if (confirm.trim().toLowerCase() === 'yes') {
        await this.db.clearAllData();
        console.log('✅ All data cleared from MongoDB');
      } else {
        console.log('❌ Operation cancelled');
      }
    } catch (error) {
      console.error(`❌ Error clearing data: ${error.message}`);
    }
  }

  async scanAndProcessFiles() {
    try {
      // Check if directory exists
      try {
        await fs.access(this.studentExcelFolder);
      } catch {
        console.log(`📁 Creating folder: ${this.studentExcelFolder}`);
        await fs.mkdir(this.studentExcelFolder, { recursive: true });
        console.log(`ℹ️  Place your Excel files in: ${this.studentExcelFolder}`);
        return false;
      }

      // Find all Excel files
      const files = await fs.readdir(this.studentExcelFolder);
      const excelFiles = files.filter(file => 
        file.endsWith('.xlsx') || file.endsWith('.xls')
      );

      if (excelFiles.length === 0) {
        console.log(`⚠️  No Excel files found in: ${this.studentExcelFolder}`);
        console.log(`ℹ️  Place your Excel files there and run again`);
        return false;
      }

      console.log(`\n📊 Found ${excelFiles.length} Excel file(s)`);
      let totalProcessed = 0;

      for (const excelFile of excelFiles) {
        const filePath = path.join(this.studentExcelFolder, excelFile);
        console.log(`\n📄 Processing: ${excelFile}`);
        
        try {
          const success = await StudentDataExtractor.processExcel(filePath, this.db);
          
          if (success) {
            console.log(`✅ Successfully processed: ${excelFile}`);
            totalProcessed++;
          } else {
            console.log(`⚠️  No data extracted from: ${excelFile}`);
          }
        } catch (error) {
          console.error(`❌ Error processing ${excelFile}: ${error.message}`);
        }
      }

      return totalProcessed > 0;

    } catch (error) {
      console.error(`❌ Error scanning files: ${error.message}`);
      return false;
    }
  }

  async showStatistics() {
    const stats = await this.db.getStatistics();

    console.log('\n' + '='.repeat(60));
    console.log('📊 SYSTEM STATISTICS');
    console.log('='.repeat(60));
    console.log(`Total Students: ${stats.total_students}`);
    console.log(`Pending Media: ${stats.pending_media}`);
    console.log(`Average Completion: ${stats.average_completion.toFixed(1)}%`);

    if (Object.keys(stats.by_department).length > 0) {
      console.log('\nBy Department:');
      Object.entries(stats.by_department).forEach(([dept, count]) => {
        console.log(`  • ${dept}: ${count} students`);
      });
    }
  }

  async showPendingMedia() {
    const pending = await this.db.getPendingMediaStudents();

    if (pending.length === 0) {
      console.log('\n✅ No students waiting for media!');
      return;
    }

    console.log('\n' + '='.repeat(60));
    console.log(`⏳ STUDENTS WAITING FOR MEDIA (${pending.length})`);
    console.log('='.repeat(60));

    pending.forEach((student, index) => {
      console.log(`\n${index + 1}. ${student.full_name || 'N/A'} (${student.student_id})`);
      console.log(`   Course: ${student.course} | Year: ${student.year} | Section: ${student.section}`);

      const waiting = [];
      if (student.waiting_for.image) waiting.push('📸 Image');
      if (student.waiting_for.audio) waiting.push('🎤 Audio');

      console.log(`   Waiting for: ${waiting.join(', ')}`);
    });
  }

async searchStudents() {
  console.log('\n' + '='.repeat(60));
  console.log('🔍 STUDENT SEARCH');
  console.log('='.repeat(60));

  const query = (await this.prompt('\nEnter search query (name or ID): ')).trim();

  if (!query) {
    console.log('❌ Please enter a search query');
    return;
  }

  // Build filters conversationally
  const filters = {};
  
  console.log('\n💬 Let me help you narrow down the search...');
  
  // Ask for department
  console.log('\nWhich department? (or press Enter to search all)');
  console.log('  Options: CCS, CHTM, CBA, CTE');
  const department = (await this.prompt('Department: ')).trim().toUpperCase() || null;
  if (department) filters.department = department;
  
  // Ask for course
  const course = (await this.prompt('Which course? (e.g., BSCS, or press Enter to skip): ')).trim().toUpperCase() || null;
  if (course) filters.course = course;
  
  // Ask for year
  const year = (await this.prompt('Which year? (1-4, or press Enter to skip): ')).trim() || null;
  if (year) filters.year = year;
  
  // Ask for section
  const section = (await this.prompt('Which section? (A, B, C, or press Enter to skip): ')).trim().toUpperCase() || null;
  if (section) filters.section = section;

  // Show what we're searching for
  console.log('\n🔎 Searching for:', query);
  if (Object.keys(filters).length > 0) {
    console.log('📋 Filters applied:');
    if (filters.department) console.log(`   Department: ${filters.department}`);
    if (filters.course) console.log(`   Course: ${filters.course}`);
    if (filters.year) console.log(`   Year: ${filters.year}`);
    if (filters.section) console.log(`   Section: ${filters.section}`);
  } else {
    console.log('📋 No filters - searching all students');
  }

  // Search with filters
  const results = await this.db.searchStudents(query, Object.keys(filters).length > 0 ? filters : null);
  const displayResults = this.db.getStudentsDisplay(results);

  if (displayResults.length === 0) {
    console.log('\n❌ No students found with these criteria');
    
    // Offer to search without filters
    const searchAgain = await this.prompt('\n💡 Want to search without filters? (yes/no): ');
    if (searchAgain.trim().toLowerCase() === 'yes') {
      const allResults = await this.db.searchStudents(query, null);
      const allDisplayResults = this.db.getStudentsDisplay(allResults);
      
      if (allDisplayResults.length === 0) {
        console.log('\n❌ No students found at all with that search term');
        return;
      }
      
      console.log(`\n✅ Found ${allDisplayResults.length} student(s) matching "${query}" (all departments):`);
      console.log('='.repeat(60));

      allDisplayResults.forEach((student, index) => {
        console.log(`\n${index + 1}. ${student.full_name || 'N/A'} (ID: ${student.student_id})`);
        console.log(`   Course: ${student.course} | Year: ${student.year} | Section: ${student.section}`);
        console.log(`   Department: ${student.department}`);
        console.log(`   Completion: ${student.completion_percentage.toFixed(1)}%`);
        
        // Show media with default indicator
        const imageDisplay = student.image?.is_default ? '📸 default image' : `📸 ${student.image?.status || 'waiting'}`;
        const audioDisplay = student.audio?.is_default ? '🎤 no audio' : `🎤 ${student.audio?.status || 'waiting'}`;
        console.log(`   Media: ${imageDisplay} | ${audioDisplay}`);
        
        // Show image path
        if (student.image?.display_path) {
          console.log(`   Image: ${student.image.display_path}`);
        }
      });
    }
    return;
  }

  // Show results
  console.log(`\n✅ Found ${displayResults.length} student(s):`);
  console.log('='.repeat(60));

  displayResults.forEach((student, index) => {
    console.log(`\n${index + 1}. ${student.full_name || 'N/A'} (ID: ${student.student_id})`);
    console.log(`   Course: ${student.course} | Year: ${student.year} | Section: ${student.section}`);
    console.log(`   Department: ${student.department}`);
    console.log(`   Completion: ${student.completion_percentage.toFixed(1)}%`);

    // Show media with default indicator
    const imageDisplay = student.image?.is_default ? '📸 default image' : `📸 ${student.image?.status || 'waiting'}`;
    const audioDisplay = student.audio?.is_default ? '🎤 no audio' : `🎤 ${student.audio?.status || 'waiting'}`;
    console.log(`   Media: ${imageDisplay} | ${audioDisplay}`);
    
    // Show image path
    if (student.image?.display_path) {
      console.log(`   Image: ${student.image.display_path}`);
    }
  });
  
  // If multiple results, offer to refine
  if (displayResults.length > 5) {
    console.log(`\n💡 Showing ${displayResults.length} results. You can search again with more specific filters to narrow it down.`);
  }
}

  async manualEntry() {
    console.log('\n' + '='.repeat(60));
    console.log('✏️  MANUAL STUDENT ENTRY');
    console.log('='.repeat(60));

    console.log('\nEnter student information:');

    const studentData = {
      student_id: (await this.prompt('Student ID: ')).trim(),
      surname: this.titleCase((await this.prompt('Surname: ')).trim()),
      first_name: this.titleCase((await this.prompt('First Name: ')).trim()),
      course: (await this.prompt('Course (e.g., BSCS): ')).trim().toUpperCase(),
      section: (await this.prompt('Section: ')).trim().toUpperCase(),
      year: (await this.prompt('Year (1-4): ')).trim(),
      contact_number: (await this.prompt('Contact Number: ')).trim(),
      guardian_name: this.titleCase((await this.prompt('Guardian Name: ')).trim()),
      guardian_contact: (await this.prompt('Guardian Contact: ')).trim()
    };

    // Create full name
    if (studentData.surname && studentData.first_name) {
      studentData.full_name = `${studentData.surname}, ${studentData.first_name}`;
    }

    // Detect department
    if (studentData.course) {
      studentData.department = StudentDataExtractor.detectDepartment(studentData.course);
    }

    // Create record
    const result = await this.db.createStudentRecord(studentData, 'manual_input');

    if (result) {
      console.log(`\n✅ Student ${result} created successfully!`);
      console.log('ℹ️  This student is now waiting for image and audio uploads');
    } else {
      console.log('\n❌ Failed to create student record');
    }
  }

  titleCase(str) {
    return str.split(' ')
      .map(word => word.charAt(0).toUpperCase() + word.slice(1).toLowerCase())
      .join(' ');
  }

async mainMenu() {
  while (true) {
    console.log('\n' + '='.repeat(60));
    console.log('🎓 SCHOOL INFORMATION SYSTEM - MONGODB');
    console.log('='.repeat(60));
    console.log('\n1. Process Excel Files');
    console.log('2. Manual Student Entry');
    console.log('3. Search Students');
    console.log('4. Show Pending Media');
    console.log('5. Show Statistics');
    console.log('6. View by Department');
    console.log('7. View Student Details');  // ← NEW
    console.log('8. Clear All Data');
    console.log('9. Exit');

    const choice = (await this.prompt('\nSelect option (1-9): ')).trim();

    try {
      if (choice === '1') {
        await this.scanAndProcessFiles();
      } else if (choice === '2') {
        await this.manualEntry();
      } else if (choice === '3') {
        await this.searchStudents();
      } else if (choice === '4') {
        await this.showPendingMedia();
      } else if (choice === '5') {
        await this.showStatistics();
      } else if (choice === '6') {
        await this.viewByDepartment();
      } else if (choice === '7') {
        await this.viewStudentDetails();  // ← NEW
      } else if (choice === '8') {
        await this.clearAllData();
      } else if (choice === '9') {
        console.log('\n👋 Goodbye!');
        break;
      } else {
        console.log('\n❌ Invalid option. Please select 1-9');
      }

      if (choice !== '9') {
        await this.prompt('\nPress Enter to continue...');
      }

    } catch (error) {
      console.error(`❌ Error: ${error.message}`);
      await this.prompt('\nPress Enter to continue...');
    }
  }
}

async viewStudentDetails() {
  console.log('\n' + '='.repeat(60));
  console.log('👤 VIEW STUDENT DETAILS');
  console.log('='.repeat(60));

  const studentId = (await this.prompt('\nEnter Student ID: ')).trim();

  if (!studentId) {
    console.log('❌ Student ID is required');
    return;
  }

  // Search for the student
  const student = await this.db.getStudentById(studentId);

  if (!student) {
    console.log(`❌ Student ${studentId} not found`);
    return;
  }

  // Display with defaults
  const displayStudent = this.db.getStudentDisplay(student);

  console.log('\n' + '='.repeat(60));
  console.log('📋 STUDENT INFORMATION');
  console.log('='.repeat(60));

  console.log(`\n🆔 Student ID: ${displayStudent.student_id}`);
  console.log(`👤 Name: ${displayStudent.full_name || 'N/A'}`);
  console.log(`   Surname: ${displayStudent.surname || 'N/A'}`);
  console.log(`   First Name: ${displayStudent.first_name || 'N/A'}`);

  console.log(`\n🎓 Academic Information:`);
  console.log(`   Department: ${displayStudent.department || 'N/A'}`);
  console.log(`   Course: ${displayStudent.course || 'N/A'}`);
  console.log(`   Year: ${displayStudent.year || 'N/A'}`);
  console.log(`   Section: ${displayStudent.section || 'N/A'}`);

  console.log(`\n📞 Contact Information:`);
  console.log(`   Contact Number: ${displayStudent.contact_number || 'N/A'}`);
  console.log(`   Guardian Name: ${displayStudent.guardian_name || 'N/A'}`);
  console.log(`   Guardian Contact: ${displayStudent.guardian_contact || 'N/A'}`);

  console.log(`\n📊 Record Status:`);
  console.log(`   Completion: ${displayStudent.completion_percentage.toFixed(1)}%`);
  console.log(`   Source: ${displayStudent.source || 'N/A'}`);
  console.log(`   Created: ${displayStudent.created_at ? new Date(displayStudent.created_at).toLocaleString() : 'N/A'}`);
  console.log(`   Updated: ${displayStudent.updated_at ? new Date(displayStudent.updated_at).toLocaleString() : 'N/A'}`);

  // Image Details
  console.log(`\n📸 Image Status:`);
  console.log(`   Status: ${displayStudent.image?.status || 'unknown'}`);
  if (displayStudent.image?.is_default) {
    console.log(`   ⚠️  Using DEFAULT IMAGE (no photo uploaded)`);
    console.log(`   Path: ${displayStudent.image.display_path}`);
  } else {
    console.log(`   ✅ Has ACTUAL IMAGE`);
    console.log(`   Filename: ${displayStudent.image?.filename || 'N/A'}`);
    console.log(`   Path: ${displayStudent.image?.display_path || 'N/A'}`);
    console.log(`   Data: ${displayStudent.image?.data ? 'Present' : 'Missing'}`);
  }

  // Audio Details
  console.log(`\n🎤 Audio Status:`);
  console.log(`   Status: ${displayStudent.audio?.status || 'unknown'}`);
  if (displayStudent.audio?.is_default) {
    console.log(`   ⚠️  NO AUDIO (not uploaded)`);
  } else {
    console.log(`   ✅ Has ACTUAL AUDIO`);
    console.log(`   Filename: ${displayStudent.audio?.filename || 'N/A'}`);
    console.log(`   Path: ${displayStudent.audio?.display_path || 'N/A'}`);
    console.log(`   Data: ${displayStudent.audio?.data ? 'Present' : 'Missing'}`);
  }

  // Descriptor
  console.log(`\n🔢 Face Descriptor:`);
  if (displayStudent.descriptor) {
    console.log(`   ✅ Present`);
    const descriptorPreview = displayStudent.descriptor.substring(0, 50);
    console.log(`   Preview: ${descriptorPreview}...`);
  } else {
    console.log(`   ⚠️  Not set`);
  }

  console.log('\n' + '='.repeat(60));
}


async viewByDepartment() {
  console.log('\n' + '='.repeat(60));
  console.log('📚 VIEW STUDENTS BY DEPARTMENT');
  console.log('='.repeat(60));
  
  console.log('\nAvailable Departments:');
  console.log('1. CCS - Computer Studies');
  console.log('2. CHTM - Hospitality & Tourism Management');
  console.log('3. CBA - Business Administration');
  console.log('4. CTE - Teacher Education');
  console.log('5. UNKNOWN - Unclassified');
  
  const choice = await this.prompt('\nSelect department (1-5): ');
  
  const deptMap = {
    '1': 'CCS',
    '2': 'CHTM',
    '3': 'CBA',
    '4': 'CTE',
    '5': 'UNKNOWN'
  };
  
  const department = deptMap[choice];
  
  if (!department) {
    console.log('❌ Invalid choice');
    return;
  }
  
  console.log(`\n📊 Statistics for ${department}:`);
  const stats = await this.db.getDepartmentStatistics(department);
  
  console.log(`Total Students: ${stats.total_students}`);
  console.log(`Average Completion: ${stats.average_completion.toFixed(1)}%`);
  
  if (stats.by_course.length > 0) {
    console.log('\nBy Course > Year > Section:');
    stats.by_course.forEach(item => {
      console.log(`  ${item._id.course} - Year ${item._id.year} - Section ${item._id.section}: ${item.count} students`);
    });
  }
  
  const viewStudents = await this.prompt('\nView all students in this department? (yes/no): ');
  
  if (viewStudents.trim().toLowerCase() === 'yes') {
    const students = await this.db.getStudentsByDepartment(department);
    
    console.log(`\n✅ ${students.length} students in ${department}:`);
    console.log('='.repeat(60));
    
    students.forEach((student, index) => {
      console.log(`\n${index + 1}. ${student.full_name || 'N/A'} (${student.student_id})`);
      console.log(`   Course: ${student.course} | Year: ${student.year} | Section: ${student.section}`);
      console.log(`   Completion: ${student.completion_percentage.toFixed(1)}%`);
    });
  }
}


  async run() {
    console.log('🎓 Starting School Information System...');
    console.log('='.repeat(60));

    try {
      // Connect to database
      await this.db.connect();

      // Show initial stats
      await this.showStatistics();

      // Start main menu
      await this.mainMenu();

    } catch (error) {
      console.error(`\n❌ System error: ${error.message}`);
    } finally {
      await this.db.close();
      this.rl.close();
      console.log('👋 Disconnected from MongoDB');
    }
  }
}

async function main() {
  try {
    // You can change connection string here
    // For local MongoDB:
    const system = new SchoolInformationSystem();

    // For MongoDB Atlas:
    // const system = new SchoolInformationSystem('mongodb+srv://user:pass@cluster.mongodb.net/school_system');

    await system.run();

  } catch (error) {
    console.error(`❌ Failed to start system: ${error.message}`);
    process.exit(1);
  }
}

// Handle Ctrl+C gracefully
process.on('SIGINT', () => {
  console.log('\n\n👋 System shutdown requested');
  process.exit(0);
});

// Run if executed directly
if (require.main === module) {
  main();
}

module.exports = SchoolInformationSystem;