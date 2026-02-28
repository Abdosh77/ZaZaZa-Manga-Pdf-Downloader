import io
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk
from urllib.parse import urlparse

import requests

ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".wepb"}
CONTENT_TYPE_TO_EXTENSION = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}
DEFAULT_PDF_NAME = "chapter.pdf"
DEFAULT_DOWNLOAD_WORKERS = 8
MAX_DOWNLOAD_WORKERS = 16


def _build_image_url(base: str, path: str) -> str:
    if path.startswith(("http://", "https://")):
        return path
    return f"{base}{path}"


def _detect_extension(img_url: str, content_type: str) -> str:
    ext = os.path.splitext(urlparse(img_url).path)[1].lower()
    if ext in ALLOWED_EXTENSIONS:
        if ext == ".jpeg":
            return ".jpg"
        if ext == ".wepb":
            return ".webp"
        return ext

    mime = content_type.split(";", maxsplit=1)[0].strip().lower()
    return CONTENT_TYPE_TO_EXTENSION.get(mime, ".jpg")


def _import_pillow():
    try:
        from PIL import Image, UnidentifiedImageError
    except Exception as exc:
        raise RuntimeError(
            "Для сборки PDF нужен Pillow. Установите зависимости: pip install -r requirements.txt"
        ) from exc
    return Image, UnidentifiedImageError


def _prepare_for_pdf(image, image_cls):
    if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
        rgba = image.convert("RGBA")
        background = image_cls.new("RGB", rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.getchannel("A"))
        rgba.close()
        return background
    return image.convert("RGB")


def _save_pdf_pages(pages, pdf_path: str) -> None:
    if not pages:
        raise ValueError("Нет изображений для сборки PDF.")
    first_page, *other_pages = pages
    first_page.save(
        pdf_path,
        "PDF",
        resolution=100.0,
        save_all=True,
        append_images=other_pages,
    )


def create_pdf_from_images(image_paths: list[str], pdf_path: str, progress_callback=None) -> None:
    image_cls, unidentified_image_error = _import_pillow()
    pages = []
    try:
        total = len(image_paths)
        for index, image_path in enumerate(image_paths, 1):
            if progress_callback:
                progress_callback(
                    f"[PDF {index}/{total}] Добавляю: {os.path.basename(image_path)}"
                )
            try:
                with image_cls.open(image_path) as img:
                    pages.append(_prepare_for_pdf(img, image_cls))
            except unidentified_image_error as exc:
                raise ValueError(f"Не удалось прочитать изображение: {image_path}") from exc

        _save_pdf_pages(pages, pdf_path)
    finally:
        for page in pages:
            page.close()


def create_pdf_from_page_bytes(
    pages_by_index: list[tuple[int, bytes]], pdf_path: str, progress_callback=None
) -> None:
    image_cls, unidentified_image_error = _import_pillow()
    pages = []
    try:
        total = len(pages_by_index)
        for position, (page_index, image_bytes) in enumerate(pages_by_index, 1):
            if progress_callback:
                progress_callback(f"[PDF {position}/{total}] Добавляю страницу #{page_index:03}")
            try:
                with image_cls.open(io.BytesIO(image_bytes)) as img:
                    pages.append(_prepare_for_pdf(img, image_cls))
            except unidentified_image_error as exc:
                raise ValueError(
                    f"Не удалось прочитать изображение страницы #{page_index:03}"
                ) from exc

        _save_pdf_pages(pages, pdf_path)
    finally:
        for page in pages:
            page.close()


def download_manga(
    url: str,
    output_dir: str = "manga",
    create_pdf: bool = False,
    pdf_filename: str = DEFAULT_PDF_NAME,
    pdf_only: bool = False,
    max_workers: int = DEFAULT_DOWNLOAD_WORKERS,
    progress_callback=None,
) -> dict:
    headers = {"User-Agent": "Mozilla/5.0"}
    os.makedirs(output_dir, exist_ok=True)
    if pdf_only and not create_pdf:
        raise ValueError("Режим 'Только PDF' требует включенной сборки PDF.")

    with requests.Session() as session:
        session.headers.update(headers)
        response = session.get(url, timeout=20)
        response.raise_for_status()
        html = response.text

        pattern = r"rm_h\.readerInit\([^,]+,\s*(\[\[.*?\]\])"
        match = re.search(pattern, html, re.S)
        if not match:
            raise ValueError("Массив страниц не найден. Вероятно, формат сайта изменился.")

        data = match.group(1)
        images = re.findall(r"\['(https?://[^']+)','',\"([^\"]+)\"", data)
        if not images:
            raise ValueError("Не удалось извлечь ссылки на изображения.")

        failed = []
        saved_by_index = {}
        page_bytes_by_index = {}
        total = len(images)
        try:
            workers_input = int(max_workers)
        except (TypeError, ValueError):
            workers_input = DEFAULT_DOWNLOAD_WORKERS
        effective_workers = max(1, min(workers_input, min(MAX_DOWNLOAD_WORKERS, total)))

        if progress_callback:
            progress_callback(
                f"Параллельная загрузка: {effective_workers} поток(ов), страниц: {total}"
            )

        thread_local = threading.local()

        def _get_thread_session() -> requests.Session:
            if not hasattr(thread_local, "session"):
                worker_session = requests.Session()
                worker_session.headers.update(headers)
                thread_local.session = worker_session
            return thread_local.session

        def _download_one(
            index: int, base: str, path: str
        ) -> tuple[int, str | None, bytes | None, str | None]:
            img_url = _build_image_url(base, path)
            try:
                worker_session = _get_thread_session()
                img_response = worker_session.get(img_url, timeout=20)
                img_response.raise_for_status()
                image_content = img_response.content
                if pdf_only:
                    return index, None, image_content, None

                extension = _detect_extension(
                    img_url, img_response.headers.get("Content-Type", "")
                )
                filename = os.path.join(output_dir, f"{index:03}{extension}")
                with open(filename, "wb") as file_obj:
                    file_obj.write(image_content)
                return index, filename, None, None
            except Exception as exc:
                return index, None, None, f"{img_url} ({exc})"

        with ThreadPoolExecutor(max_workers=effective_workers) as executor:
            futures = {
                executor.submit(_download_one, index, base, path): index
                for index, (base, path) in enumerate(images, 1)
            }

            done = 0
            for future in as_completed(futures):
                index, filename, image_content, error = future.result()
                done += 1
                if error:
                    failed.append(error)
                    if progress_callback:
                        progress_callback(f"[{done}/{total}] Ошибка страницы #{index:03}")
                    continue

                if pdf_only:
                    page_bytes_by_index[index] = image_content
                    if progress_callback:
                        progress_callback(f"[{done}/{total}] Получено: #{index:03}")
                else:
                    saved_by_index[index] = filename
                    if progress_callback:
                        progress_callback(
                            f"[{done}/{total}] Сохранено: {os.path.basename(filename)}"
                        )

        saved_files = [saved_by_index[idx] for idx in sorted(saved_by_index)]
        page_bytes = [(idx, page_bytes_by_index[idx]) for idx in sorted(page_bytes_by_index)]
        saved = len(page_bytes) if pdf_only else len(saved_files)

    pdf_path = None
    pdf_error = None
    if create_pdf:
        if not pdf_filename.lower().endswith(".pdf"):
            pdf_filename = f"{pdf_filename}.pdf"
        pdf_path = os.path.join(output_dir, pdf_filename)
        if saved > 0:
            if progress_callback:
                progress_callback("Собираю страницы в PDF...")
            try:
                if pdf_only:
                    create_pdf_from_page_bytes(page_bytes, pdf_path, progress_callback)
                else:
                    create_pdf_from_images(saved_files, pdf_path, progress_callback)
            except Exception as exc:
                pdf_error = str(exc)
        else:
            pdf_error = "Нет загруженных изображений для сборки PDF."

    return {
        "total": total,
        "saved": saved,
        "failed": failed,
        "pdf_path": pdf_path,
        "pdf_error": pdf_error,
        "pdf_requested": create_pdf,
        "pdf_only": pdf_only,
    }


class MangaDownloaderApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("ZaZaZa Manga Downloader")
        self.root.geometry("880x560")
        self.root.minsize(700, 440)

        self.url_var = tk.StringVar()
        self.output_var = tk.StringVar(value="manga")
        self.make_pdf_var = tk.BooleanVar(value=True)
        self.pdf_only_var = tk.BooleanVar(value=False)
        self.pdf_name_var = tk.StringVar(value=DEFAULT_PDF_NAME)
        self.workers_var = tk.StringVar(value=str(DEFAULT_DOWNLOAD_WORKERS))
        self.status_var = tk.StringVar(value="Готово к скачиванию.")

        container = ttk.Frame(root, padding=12)
        container.pack(fill="both", expand=True)
        container.columnconfigure(0, weight=1)
        container.rowconfigure(5, weight=1)

        ttk.Label(container, text="Ссылка на главу:").grid(
            row=0, column=0, sticky="w", pady=(0, 4)
        )
        self.url_entry = ttk.Entry(container, textvariable=self.url_var)
        self.url_entry.grid(row=1, column=0, sticky="ew", padx=(0, 8))

        self.download_button = ttk.Button(
            container, text="Скачать", command=self.start_download
        )
        self.download_button.grid(row=1, column=1, sticky="ew")

        ttk.Label(container, text="Папка для сохранения:").grid(
            row=2, column=0, sticky="w", pady=(12, 4)
        )
        self.output_entry = ttk.Entry(container, textvariable=self.output_var)
        self.output_entry.grid(row=3, column=0, columnspan=2, sticky="ew")

        options = ttk.Frame(container)
        options.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        options.columnconfigure(2, weight=1)
        options.columnconfigure(4, weight=0)

        self.pdf_check = ttk.Checkbutton(
            options, text="Собрать в один PDF", variable=self.make_pdf_var
        )
        self.pdf_check.grid(row=0, column=0, sticky="w")

        ttk.Label(options, text="Имя PDF:").grid(row=0, column=1, sticky="e", padx=(14, 6))
        self.pdf_name_entry = ttk.Entry(options, textvariable=self.pdf_name_var)
        self.pdf_name_entry.grid(row=0, column=2, sticky="ew")

        ttk.Label(options, text="Потоков:").grid(
            row=0, column=3, sticky="e", padx=(14, 6)
        )
        self.workers_entry = ttk.Entry(options, textvariable=self.workers_var, width=5)
        self.workers_entry.grid(row=0, column=4, sticky="w")

        self.pdf_only_check = ttk.Checkbutton(
            options, text="Только PDF (без отдельных картинок)", variable=self.pdf_only_var
        )
        self.pdf_only_check.grid(row=1, column=0, columnspan=5, sticky="w", pady=(8, 0))

        self.log_box = scrolledtext.ScrolledText(container, wrap="word", state="disabled")
        self.log_box.grid(row=5, column=0, columnspan=2, sticky="nsew", pady=(12, 0))

        ttk.Label(container, textvariable=self.status_var).grid(
            row=6, column=0, columnspan=2, sticky="w", pady=(10, 0)
        )

    def _append_log(self, message: str) -> None:
        self.log_box.configure(state="normal")
        self.log_box.insert(tk.END, f"{message}\n")
        self.log_box.see(tk.END)
        self.log_box.configure(state="disabled")

    def _append_log_threadsafe(self, message: str) -> None:
        self.root.after(0, self._append_log, message)

    def _set_busy(self, is_busy: bool) -> None:
        state = "disabled" if is_busy else "normal"
        self.download_button.configure(state=state)
        self.url_entry.configure(state=state)
        self.output_entry.configure(state=state)
        self.pdf_check.configure(state=state)
        self.pdf_only_check.configure(state=state)
        self.pdf_name_entry.configure(state=state)
        self.workers_entry.configure(state=state)

    def start_download(self) -> None:
        url = self.url_var.get().strip()
        output_dir = self.output_var.get().strip() or "manga"
        make_pdf = bool(self.make_pdf_var.get())
        pdf_only = bool(self.pdf_only_var.get())
        pdf_name = self.pdf_name_var.get().strip() or DEFAULT_PDF_NAME
        workers_raw = self.workers_var.get().strip() or str(DEFAULT_DOWNLOAD_WORKERS)
        if not url:
            messagebox.showwarning("Пустая ссылка", "Вставьте ссылку на главу манги.")
            return
        if pdf_only and not make_pdf:
            messagebox.showwarning(
                "Некорректные параметры",
                "Режим 'Только PDF' работает только при включенной сборке PDF.",
            )
            return
        try:
            max_workers = int(workers_raw)
        except ValueError:
            messagebox.showwarning(
                "Некорректное значение",
                f"Количество потоков должно быть числом от 1 до {MAX_DOWNLOAD_WORKERS}.",
            )
            return

        max_workers = max(1, min(MAX_DOWNLOAD_WORKERS, max_workers))
        self.workers_var.set(str(max_workers))

        self._set_busy(True)
        self.status_var.set("Идет скачивание...")
        self._append_log(f"Старт: {url}")
        self._append_log(f"Потоков загрузки: {max_workers}")
        if make_pdf:
            self._append_log(f"PDF после скачивания: {pdf_name}")
        if pdf_only:
            self._append_log("Режим: только PDF (отдельные картинки не сохраняются)")

        worker = threading.Thread(
            target=self._download_worker,
            args=(url, output_dir, make_pdf, pdf_name, pdf_only, max_workers),
            daemon=True,
        )
        worker.start()

    def _download_worker(
        self,
        url: str,
        output_dir: str,
        make_pdf: bool,
        pdf_name: str,
        pdf_only: bool,
        max_workers: int,
    ) -> None:
        try:
            result = download_manga(
                url=url,
                output_dir=output_dir,
                create_pdf=make_pdf,
                pdf_filename=pdf_name,
                pdf_only=pdf_only,
                max_workers=max_workers,
                progress_callback=self._append_log_threadsafe,
            )
            self.root.after(0, self._handle_success, result, output_dir)
        except Exception as exc:
            self.root.after(0, self._handle_error, exc)

    def _handle_success(self, result: dict, output_dir: str) -> None:
        self._set_busy(False)

        if result["failed"]:
            self._append_log("Список ошибок загрузки:")
            for item in result["failed"]:
                self._append_log(f"- {item}")
        if result["pdf_error"]:
            self._append_log(f"Ошибка сборки PDF: {result['pdf_error']}")
        if result["pdf_path"] and not result["pdf_error"]:
            self._append_log(f"PDF сохранен: {result['pdf_path']}")

        has_issues = bool(result["failed"] or result["pdf_error"])
        if has_issues:
            self.status_var.set(
                f"Готово с ошибками: {result['saved']} из {result['total']} страниц."
            )
            message = (
                f"Скачано {result['saved']} из {result['total']} страниц.\n"
                f"Папка: {output_dir}"
            )
            if result["pdf_path"] and not result["pdf_error"]:
                message += f"\nPDF: {result['pdf_path']}"
            if result["pdf_error"]:
                message += f"\nОшибка PDF: {result['pdf_error']}"
            messagebox.showwarning("Завершено с ошибками", message)
            return

        if result["pdf_requested"] and result["pdf_path"]:
            if result.get("pdf_only"):
                self.status_var.set(f"Готово: {result['saved']} страниц в PDF.")
            else:
                self.status_var.set(f"Готово: {result['saved']} страниц + PDF.")
            messagebox.showinfo(
                "Готово",
                (
                    f"Скачивание завершено.\nСтраниц: {result['saved']}\n"
                    f"PDF: {result['pdf_path']}\nПапка: {output_dir}"
                    + ("\nОтдельные картинки не сохранялись." if result.get("pdf_only") else "")
                ),
            )
            return

        self.status_var.set(f"Готово: {result['saved']} страниц.")
        messagebox.showinfo(
            "Готово",
            f"Скачивание завершено.\nСтраниц: {result['saved']}\nПапка: {output_dir}",
        )

    def _handle_error(self, error: Exception) -> None:
        self._set_busy(False)
        self.status_var.set("Ошибка при скачивании.")
        self._append_log(f"Критическая ошибка: {error}")
        messagebox.showerror("Ошибка", str(error))


def main() -> None:
    root = tk.Tk()
    app = MangaDownloaderApp(root)
    app.url_entry.focus_set()
    root.mainloop()


if __name__ == "__main__":
    main()
