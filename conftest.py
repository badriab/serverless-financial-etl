import sys
import os

# Add each lambda directory explicitly — order matters
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "lambdas", "transformer"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "lambdas", "ingestor"))