"""
Image and Audio Upload System for Students
Upload media files and store them in MongoDB using GridFS
"""

import os
import base64
from pathlib import Path
from main import StudentDatabase
import gridfs
from pymongo import MongoClient

class MediaUploader:
    def __init__(self, connection_string=None, database_name="school_system"):
        """Initialize media uploader with GridFS support"""
        if connection_string is None:
            connection_string = "mongodb://localhost:27017/"
        
        self.client = MongoClient(connection_string)
        self.db = self.client[database_name]
        self.student_db = StudentDatabase(connection_string, database_name)
        
        # GridFS for storing large files
        self.fs = gridfs.GridFS(self.db)
        
        print("✅ Media uploader initialized")
    
    def upload_image_base64(self, student_id, image_path):
        """
        Upload image as base64 (for small images < 1MB)
        Simple method - stores directly in student document
        """
        try:
            # Read image file
            with open(image_path, 'rb') as img_file:
                image_data = img_file.read()
            
            # Convert to base64
            image_base64 = base64.b64encode(image_data).decode('utf-8')
            
            # Get file info
            file_size = len(image_data)
            filename = os.path.basename(image_path)
            
            print(f"📸 Uploading image: {filename}")
            print(f"   Size: {file_size / 1024:.2f} KB")
            print(f"   Student ID: {student_id}")
            
            # Update student record
            success = self.student_db.update_media(
                student_id=student_id,
                media_type='image',
                media_data=image_base64,
                filename=filename
            )
            
            if success:
                print(f"✅ Image uploaded successfully!")
                return True
            else:
                print(f"❌ Failed to update student record")
                return False
                
        except FileNotFoundError:
            print(f"❌ Image file not found: {image_path}")
            return False
        except Exception as e:
            print(f"❌ Error uploading image: {e}")
            return False
    
    def upload_image_gridfs(self, student_id, image_path):
        """
        Upload image using GridFS (for larger files)
        Better for files > 1MB - stores in separate chunks
        """
        try:
            # Read image file
            with open(image_path, 'rb') as img_file:
                image_data = img_file.read()
            
            filename = os.path.basename(image_path)
            file_size = len(image_data)
            
            print(f"📸 Uploading image to GridFS: {filename}")
            print(f"   Size: {file_size / 1024:.2f} KB")
            print(f"   Student ID: {student_id}")
            
            # Store in GridFS
            file_id = self.fs.put(
                image_data,
                filename=filename,
                student_id=student_id,
                content_type='image/jpeg'  # or detect from file extension
            )
            
            print(f"   GridFS ID: {file_id}")
            
            # Update student record with GridFS reference
            success = self.student_db.update_media(
                student_id=student_id,
                media_type='image',
                media_data=str(file_id),  # Store GridFS ID
                filename=filename
            )
            
            if success:
                print(f"✅ Image uploaded to GridFS successfully!")
                return file_id
            else:
                print(f"❌ Failed to update student record")
                return None
                
        except FileNotFoundError:
            print(f"❌ Image file not found: {image_path}")
            return None
        except Exception as e:
            print(f"❌ Error uploading image: {e}")
            return None
    
    def upload_audio(self, student_id, audio_path, use_gridfs=True):
        """Upload audio file (recommended to use GridFS for audio)"""
        try:
            # Read audio file
            with open(audio_path, 'rb') as audio_file:
                audio_data = audio_file.read()
            
            filename = os.path.basename(audio_path)
            file_size = len(audio_data)
            
            print(f"🎤 Uploading audio: {filename}")
            print(f"   Size: {file_size / 1024:.2f} KB")
            print(f"   Student ID: {student_id}")
            
            if use_gridfs:
                # Store in GridFS (recommended for audio)
                file_id = self.fs.put(
                    audio_data,
                    filename=filename,
                    student_id=student_id,
                    content_type='audio/mpeg'  # adjust based on file type
                )
                
                media_data = str(file_id)
                print(f"   GridFS ID: {file_id}")
            else:
                # Store as base64
                media_data = base64.b64encode(audio_data).decode('utf-8')
            
            # Update student record
            success = self.student_db.update_media(
                student_id=student_id,
                media_type='audio',
                media_data=media_data,
                filename=filename
            )
            
            if success:
                print(f"✅ Audio uploaded successfully!")
                return True
            else:
                print(f"❌ Failed to update student record")
                return False
                
        except FileNotFoundError:
            print(f"❌ Audio file not found: {audio_path}")
            return False
        except Exception as e:
            print(f"❌ Error uploading audio: {e}")
            return False
    
    def retrieve_image(self, student_id, save_to=None):
        """Retrieve and optionally save image"""
        try:
            student = self.student_db.get_student_by_id(student_id)
            
            if not student:
                print(f"❌ Student {student_id} not found")
                return None
            
            image_data = student.get('image', {}).get('data')
            filename = student.get('image', {}).get('filename', 'image.jpg')
            
            if not image_data:
                print(f"⚠️  No image data for student {student_id}")
                return None
            
            print(f"📸 Retrieving image for {student_id}")
            print(f"   Filename: {filename}")
            print(f"   Data type: {'GridFS' if len(image_data) < 100 else 'Base64'}")
            
            # Check if it's a GridFS ID or base64
            if len(image_data) < 100:  # Likely a GridFS ID
                # Retrieve from GridFS
                try:
                    from bson.objectid import ObjectId
                    # Convert string ID to ObjectId
                    file_id = ObjectId(image_data)
                    print(f"   GridFS ID: {file_id}")
                    
                    # Get file from GridFS
                    grid_out = self.fs.get(file_id)
                    image_bytes = grid_out.read()
                    
                    print(f"   Retrieved {len(image_bytes)} bytes from GridFS")
                    
                except Exception as e:
                    print(f"❌ Failed to retrieve from GridFS: {e}")
                    print(f"   Image data value: {image_data}")
                    return None
            else:
                # Decode base64
                image_bytes = base64.b64decode(image_data)
                print(f"   Decoded {len(image_bytes)} bytes from base64")
            
            if save_to:
                with open(save_to, 'wb') as f:
                    f.write(image_bytes)
                print(f"✅ Image saved to: {save_to}")
            else:
                # Auto-save to retrieved_images folder
                os.makedirs("retrieved_images", exist_ok=True)
                auto_path = os.path.join("retrieved_images", filename)
                with open(auto_path, 'wb') as f:
                    f.write(image_bytes)
                print(f"✅ Image saved to: {auto_path}")
            
            return image_bytes
            
        except Exception as e:
            print(f"❌ Error retrieving image: {e}")
            import traceback
            traceback.print_exc()
            return None
    
    def batch_upload_from_folder(self, folder_path, use_gridfs=False):
        """
        Batch upload images from a folder
        Filename should match student ID (e.g., 2024-0001.jpg)
        """
        try:
            folder = Path(folder_path)
            if not folder.exists():
                print(f"❌ Folder not found: {folder_path}")
                return
            
            # Find image files
            image_extensions = ['.jpg', '.jpeg', '.png', '.gif', '.bmp']
            image_files = [f for f in folder.iterdir() 
                          if f.suffix.lower() in image_extensions]
            
            if not image_files:
                print(f"⚠️  No image files found in: {folder_path}")
                return
            
            print(f"\n{'='*70}")
            print(f"📦 BATCH IMAGE UPLOAD")
            print('='*70)
            print(f"Found {len(image_files)} image(s)")
            
            success_count = 0
            failed_count = 0
            
            for img_file in image_files:
                # Extract student ID from filename (remove extension)
                student_id = img_file.stem
                
                print(f"\n📸 Processing: {img_file.name}")
                print(f"   Detected Student ID: {student_id}")
                
                # Check if student exists
                student = self.student_db.get_student_by_id(student_id)
                if not student:
                    print(f"   ⚠️  Student {student_id} not found in database - skipping")
                    failed_count += 1
                    continue
                
                # Upload image
                if use_gridfs:
                    result = self.upload_image_gridfs(student_id, str(img_file))
                else:
                    result = self.upload_image_base64(student_id, str(img_file))
                
                if result:
                    success_count += 1
                else:
                    failed_count += 1
            
            print(f"\n{'='*70}")
            print(f"✅ BATCH UPLOAD COMPLETE")
            print('='*70)
            print(f"Success: {success_count}/{len(image_files)}")
            print(f"Failed: {failed_count}/{len(image_files)}")
            
        except Exception as e:
            print(f"❌ Error in batch upload: {e}")
    
    def close(self):
        """Close connections"""
        self.student_db.close()
        self.client.close()


def interactive_upload():
    """Interactive media upload interface"""
    print("="*70)
    print("📸 STUDENT MEDIA UPLOAD SYSTEM")
    print("="*70)
    
    uploader = MediaUploader()
    
    while True:
        print(f"\n{'='*70}")
        print("OPTIONS:")
        print("1. Upload Single Image (Base64)")
        print("2. Upload Single Image (GridFS)")
        print("3. Upload Audio")
        print("4. Batch Upload Images from Folder")
        print("5. View Student's Image")
        print("6. Show Pending Media Students")
        print("7. Exit")
        print('='*70)
        
        choice = input("\nSelect option (1-7): ").strip()
        
        if choice == "1":
            student_id = input("\nEnter Student ID: ").strip()
            image_path = input("Enter image path: ").strip()
            uploader.upload_image_base64(student_id, image_path)
            
        elif choice == "2":
            student_id = input("\nEnter Student ID: ").strip()
            image_path = input("Enter image path: ").strip()
            uploader.upload_image_gridfs(student_id, image_path)
            
        elif choice == "3":
            student_id = input("\nEnter Student ID: ").strip()
            audio_path = input("Enter audio path: ").strip()
            uploader.upload_audio(student_id, audio_path)
            
        elif choice == "4":
            folder_path = input("\nEnter folder path containing images: ").strip()
            use_gridfs = input("Use GridFS? (yes/no): ").strip().lower() == 'yes'
            uploader.batch_upload_from_folder(folder_path, use_gridfs)
            
        elif choice == "5":
            student_id = input("\nEnter Student ID: ").strip()
            save_path = input("Save to (leave blank to auto-save): ").strip()
            save_path = save_path if save_path else None
            result = uploader.retrieve_image(student_id, save_path)
            
            if result:
                # Ask if user wants to open the image
                open_image = input("\nOpen image now? (yes/no): ").strip().lower()
                if open_image == 'yes':
                    try:
                        import os
                        # Get the saved path
                        if save_path:
                            image_path = save_path
                        else:
                            student = uploader.student_db.get_student_by_id(student_id)
                            filename = student.get('image', {}).get('filename', f'{student_id}.jpg')
                            image_path = os.path.join("retrieved_images", filename)
                        
                        # Open with default image viewer
                        os.startfile(image_path)  # Windows
                        print(f"✅ Opening image...")
                    except Exception as e:
                        print(f"❌ Could not open image: {e}")
                        print(f"   Please open manually: {image_path}")
            
        elif choice == "6":
            pending = uploader.student_db.get_pending_media_students()
            print(f"\n⏳ Students waiting for media: {len(pending)}")
            for i, student in enumerate(pending[:20], 1):
                waiting = []
                if student['waiting_for']['image']:
                    waiting.append("📸 Image")
                if student['waiting_for']['audio']:
                    waiting.append("🎤 Audio")
                print(f"{i}. {student.get('full_name', 'N/A')} ({student['student_id']}) - {', '.join(waiting)}")
            
        elif choice == "7":
            print("\n👋 Goodbye!")
            break
        else:
            print("❌ Invalid option")
        
        input("\nPress Enter to continue...")
    
    uploader.close()


if __name__ == "__main__":
    try:
        interactive_upload()
    except KeyboardInterrupt:
        print("\n\n👋 Exiting...")
    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback
        traceback.print_exc()