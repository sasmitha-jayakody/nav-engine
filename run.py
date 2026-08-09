"""
Runs the three stages in order so a fresh clone works with one command.

    python run.py
"""

from src import generate_data, calculate_nav, reconciliation

if __name__ == "__main__":
    print("\n[1/3] generating synthetic data ...")
    generate_data.main()
    print("\n[2/3] striking daily NAV per share class ...")
    calculate_nav.run()
    print("\n[3/3] running reconciliation / exceptions ...")
    reconciliation.run()
