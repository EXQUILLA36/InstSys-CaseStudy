import requests
import json
from apscheduler.schedulers.background import BackgroundScheduler # type: ignore

class Configuration:
    def __init__(self):
        self.scheduler = BackgroundScheduler()
        self.execution_mode = self.check_network()
        self.start_network_monitoring()
    
    def check_network(self):
        try:
            requests.get("https://www.google.com", timeout=3)
            return 'online'
        except requests.RequestException:
            return 'offline'
    
    def ai_config(self):
        try:
            with open("config/config.json", "r", encoding="utf-8") as f:
                self.full_config = json.load(f)
        except FileNotFoundError:
            print("config.json not found! Cannot start AI Analyst.")
            self.full_config = None
    
    def start_network_monitoring(self):
        self.scheduler.add_job(self.update_execution_mode, 'interval', seconds=5)
        self.scheduler.start()
    
    def update_execution_mode(self):
        try:
            new_mode = self.check_network()
            if new_mode != self.execution_mode:
                print(f"execution mode changed")
            self.execution_mode = new_mode
        except Exception as e:
            print(f"error")
        
    def shutdown(self):
        if self.scheduler.running:
            self.scheduler.shutdown()


    


