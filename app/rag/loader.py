def load_text_file(file_path):
    with open(file_path, "r", encoding="utf-8") as file:
        text = file.read()

    return text

if __name__ == "__main__":
    text = load_text_file("data/clinic_faq.txt")
    print(text)