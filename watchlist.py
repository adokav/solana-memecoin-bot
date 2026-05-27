
class Watchlist:
    def __init__(self):
        self.tokens={}
    def add(self,mint,data):
        self.tokens[mint]=data
