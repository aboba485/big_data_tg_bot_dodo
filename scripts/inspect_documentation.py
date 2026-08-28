from app.config import get_settings
from app.documentation import DocumentationLoader

if __name__ == "__main__":
    settings = get_settings()
    bundle = DocumentationLoader(settings.documentation_zip_path).load()
    print(f"format={bundle.format_version} operations={len(bundle.operations)}")
