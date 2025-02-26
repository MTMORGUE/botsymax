from abc import ABC, abstractmethod

class BasePlatformAdapter(ABC):
    def __init__(self, bot):
        self.bot = bot

    @abstractmethod
    def authenticate(self):
        pass

    @abstractmethod
    def post(self, content: str):
        pass

    @abstractmethod
    def comment(self, content: str, reply_to_id: str):
        pass

    @abstractmethod
    def dm(self, recipient: str, message: str):
        pass