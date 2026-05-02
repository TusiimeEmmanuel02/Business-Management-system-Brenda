# Main entry point for the Brenda's Printing Hub Business Management System

if __name__ == '__main__':
    import sys
    from PySide6.QtWidgets import QApplication
    from ui.main_window import MainWindow

    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
