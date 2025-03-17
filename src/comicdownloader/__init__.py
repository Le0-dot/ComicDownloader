import re
import io
import sys
import asyncio
from math import ceil
from pathlib import Path
from types import TracebackType
from zipfile import ZipFile
from argparse import ArgumentParser, BooleanOptionalAction
from dataclasses import dataclass, field
from curl_cffi.requests import AsyncSession
from selectolax.parser import HTMLParser, Node
from PIL import Image


class Cleared:
    def __init__(self, *args) -> None:
        self._data = "\n".join(args)
        self._lines = self._data.count("\n") + 1 if args else 0

    def __str__(self) -> str:
        return self._data

    def clear_str(self) -> str:
        return "\033[1A\x1b[2K" * self._lines


@dataclass
class Img:
    filename: str
    url: str

    async def download_jpeg(self, session: AsyncSession) -> bytes:
        try:
            response = await session.get(self.url, stream=True)
            data = await response.acontent()
            image = Image.open(io.BytesIO(data))
        except Exception as e:
            raise RuntimeError(
                f"Encountered an error while downloading {self.url!r}"
            ) from e

        buffer = io.BytesIO()
        image.save(buffer, format="jpeg")
        return buffer.getvalue()


@dataclass
class Archive:
    filename: str
    imgs: list[Img]
    _imgs_data: list[bytes] = field(init=False)

    async def download(self, session: AsyncSession) -> None:
        tasks = [img.download_jpeg(session) for img in self.imgs]
        self._imgs_data = await asyncio.gather(*tasks)

    def save(self, directory: Path):
        with ZipFile(directory / self.filename, "w") as archive:
            for img, data in zip(self.imgs, self._imgs_data):
                archive.writestr(img.filename, data)


@dataclass
class ImageConfig:
    selector: str
    number_attr: str
    url_attr: str
    number_pattern: re.Pattern


def img_from_node(node: Node, config: ImageConfig) -> Img | None:
    number = node.attributes.get(config.number_attr)
    url = node.attributes.get(config.url_attr)

    if not url or not number:
        print(
            f"Url or number for image is not present: url = {url!r}, number = {number!r}"
        )
        return None
    if url.endswith("png"):
        print(f"Image with url = {url!r} will be dropped")
        return None
    if (match := re.search(config.number_pattern, number)) is None:
        print(f"No number found for image {url!r} in {number!r}")
        return None

    return Img(f"{match.group(1):0>3}.jpg", url.strip())


async def find_images(
    url: str, config: ImageConfig, session: AsyncSession
) -> list[Img]:
    response = await session.get(url)
    nodes = HTMLParser(response.text).css(config.selector)
    images = (img_from_node(node, config) for node in nodes)
    return [image for image in images if image is not None]


def sequential_search(data: str, regexs: list[re.Pattern]) -> str | None:
    for regex in regexs:
        if (match := re.search(regex, data)) is None:
            return None
        data = match.group(1)
    return data


def get_name(url: str, regexs: list[re.Pattern], padding: int | None) -> str:
    if (name := sequential_search(url, regexs)) is None:
        raise ValueError(f"Regular Expression for name did not match for {url!r}")
    if padding:
        return f"{name:0>{padding}}.cbz"
    return f"{name}.cbz"


def bar(
    completed: int, overall: int, width: int = 100, filler: str = "█", empty: str = " "
) -> str:
    step = 1 / overall * width
    taken = ceil(completed * step)
    return f"[{filler * taken}{empty * (width - taken)}]"


def custom_exception_hook(
    exception_type: type[BaseException],
    exception: BaseException,
    traceback: TracebackType | None,
) -> None:
    print(f"{exception}")


def main():
    parser = ArgumentParser(
        prog="ComicDownloader",
        description="Given an URLs to a website with images, download and create cbz file from them.",
    )

    parser.add_argument("URLs", nargs="+", help="urls with images to download")
    parser.add_argument(
        "-u",
        "--url-name-pattern",
        nargs="?",
        default=r"^.+/([^/]+)/?$",
        help="a python regular expression with 1 capture group, that will be used to get filename for archive (default: filename in url)",
    )
    parser.add_argument(
        "-d",
        "--directory",
        nargs="?",
        default=".",
        type=Path,
        help="directory to put output file into (default: .)",
    )
    parser.add_argument(
        "-p",
        "--padding",
        nargs="?",
        default=3,
        type=int,
        help="treat filename extracted with --url-name as a number and pad it to specified length (default: 3)",
    )
    parser.add_argument(
        "-n",
        "--number",
        action=BooleanOptionalAction,
        default=True,
        help="find a number in name extracted by --url-name and use it (default: --number)",
    )
    parser.add_argument(
        "--image-selector",
        default="img",
        help="css selector to find correct images on web page (default: img)",
    )
    parser.add_argument(
        "--number-attr",
        default="id",
        help="HTML attribute of <img> tag contraining number of the image (default: id)",
    )
    parser.add_argument(
        "--url-attr",
        default="src",
        help="HTML attribute of <img> tag contraining url of the image (default: src)",
    )
    parser.add_argument(
        "--number-pattern",
        default=r"(\d+)",
        help="regular expression pattern to extract a number from attribute specified by --number_attr (default: ([0-9]+))",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="show more detailed information"
    )

    args = parser.parse_args()

    if not args.verbose:
        sys.excepthook = custom_exception_hook

    if not args.directory.exists():
        args.directory.mkdir()
    if not args.directory.is_dir():
        parser.exit(status=1, message=f"{args.directory} is not a directory")

    regexs = [re.compile(args.url_name_pattern)]
    if args.number:
        regexs.append(re.compile(r"(\d+)"))

    config = ImageConfig(
        selector=args.image_selector,
        number_attr=args.number_attr,
        url_attr=args.url_attr,
        number_pattern=args.number_pattern,
    )

    async def actions():
        async with AsyncSession(impersonate="chrome") as session:
            msg = Cleared()
            for idx, url in enumerate(args.URLs):
                msg = Cleared(bar(idx, len(args.URLs)), f"Processing {url!r}")
                print(msg)
                images = await find_images(url, config, session)
                if len(images) == 0:
                    raise RuntimeError(f"No images found for {url!r}")
                archive = Archive(get_name(url, regexs, args.padding), images)
                await archive.download(session)
                archive.save(args.directory)
                print(msg.clear_str(), end="")
            print(bar(len(args.URLs), len(args.URLs)))

    asyncio.run(actions())


if __name__ == "__main__":
    main()
