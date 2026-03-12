# maunium-stickerpicker - A fast and simple Matrix sticker picker widget.
# Copyright (C) 2020 Tulir Asokan
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
from typing import Dict, Tuple
import argparse
import asyncio
import os.path
import json
import re

from telethon import TelegramClient
from telethon.tl.functions.messages import GetAllStickersRequest, GetStickerSetRequest
from telethon.tl.types.messages import AllStickers
from telethon.tl.types import InputStickerSetShortName, Document, DocumentAttributeSticker
from telethon.tl.types.messages import StickerSet as StickerSetFull

from .lib import matrix, util


def create_fallback_png() -> bytes:
    from io import BytesIO
    from PIL import Image
    img = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
    buf = BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def convert_webm_to_gif(webm_data: bytes) -> bytes:
    import subprocess
    from io import BytesIO
    import tempfile
    import os

    with tempfile.NamedTemporaryFile(suffix=".webm", delete=False) as f:
        f.write(webm_data)
        webm_path = f.name

    gif_path = webm_path.replace(".webm", ".gif")

    try:
        subprocess.run([
            "ffmpeg", "-i", webm_path,
            "-vf", "fps=15,scale=256:-1:flags=lanczos,split[s0][s1];[s0]palettegen[p];[s1][p]paletteuse",
            "-loop", "0", gif_path
        ], capture_output=True, check=True)

        with open(gif_path, "rb") as f:
            return f.read()
    finally:
        os.unlink(webm_path)
        if os.path.exists(gif_path):
            os.unlink(gif_path)


def convert_tgs_to_gif(tgs_data: bytes) -> bytes:
    import gzip
    import json
    from io import BytesIO
    from PIL import Image

    json_data = gzip.decompress(tgs_data)
    anim = json.loads(json_data)

    width, height = 256, 256
    frames = []

    if "layers" in anim:
        layers = anim["layers"]
    elif "frg" in anim:
        layers = anim["frg"].get("layers", [])
    else:
        layers = []

    durations = anim.get("op", 3000) / 1000
    frame_count = len(layers) if layers else 1

    for i, layer in enumerate(layers):
        img = Image.new("RGBA", (width, height), (0, 0, 0, 0))

        if "fr" in layer:
            for shape in layer["fr"]:
                if "x" in shape and "y" in shape:
                    color = shape.get("c", [0, 0, 0, 255])
                    points = shape.get("v", [])
                    if len(points) >= 3:
                        from PIL import ImageDraw
                        draw = ImageDraw.Draw(img)
                        poly = [(p["x"], p["y"]) for p in points]
                        draw.polygon(poly, fill=tuple(color[:4]))

        frames.append(img)

    if not frames:
        frames = [Image.new("RGBA", (width, height), (0, 0, 0, 0))]

    output = BytesIO()
    frames[0].save(
        output,
        format="GIF",
        save_all=True,
        append_images=frames[1:] if len(frames) > 1 else [],
        duration=int(durations * 1000 / max(frame_count, 1)),
        loop=0,
        disposal=2
    )
    return output.getvalue()


async def reupload_document(client: TelegramClient, document: Document) -> Tuple[matrix.StickerInfo, bytes] | None:
    print(f"Reuploading {document.id}", end="", flush=True)
    raw_data = await client.download_media(document, file=bytes)
    if not raw_data:
        raise ValueError(f"Failed to download document {document.id}")
    data: bytes = raw_data if isinstance(raw_data, bytes) else raw_data.encode()
    print(".", end="", flush=True)

    is_gif = len(data) >= 6 and (data[:6] == b"GIF87a" or data[:6] == b"GIF89a")
    is_webp = len(data) >= 4 and data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    is_webm = len(data) >= 4 and data[:4] == b"\x1a\x45\xdf\xa3"
    is_png = len(data) >= 8 and data[:8] == b"\x89PNG\r\n\x1a\n"
    is_jpeg = len(data) >= 2 and data[:2] == b"\xff\xd8"
    is_tgs = len(data) >= 2 and data[:2] == b"\x1f\x8b"

    if is_gif:
        width, height = 256, 256
        ext = "gif"
        mimetype = "image/gif"
    elif is_webm:
        print("(converting webm to gif)", end="", flush=True)
        try:
            data = convert_webm_to_gif(data)
        except Exception as e:
            print(f"Warning: could not convert webm: {e}, using fallback")
            data = create_fallback_png()
        width, height = 256, 256
        ext = "gif"
        mimetype = "image/gif"
    elif is_tgs:
        print("(converting tgs to gif)", end="", flush=True)
        try:
            data = convert_tgs_to_gif(data)
        except Exception as e:
            print(f"Warning: could not convert tgs: {e}, skipping")
            return None
        width, height = 256, 256
        ext = "gif"
        mimetype = "image/gif"
    elif is_webp:
        width, height = 256, 256
        ext = "webp"
        mimetype = "image/webp"
    elif is_png or is_jpeg:
        try:
            data, width, height = util.convert_image(data)
        except Exception as e:
            print(f"Warning: could not convert image: {e}, using fallback")
            from io import BytesIO
            from PIL import Image
            img = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
            buf = BytesIO()
            img.save(buf, "PNG")
            data = buf.getvalue()
            width, height = 256, 256
        ext = "png"
        mimetype = "image/png"
    else:
        print(f"Warning: unknown format, header: {data[:16].hex()}, using fallback")
        from io import BytesIO
        from PIL import Image
        img = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
        buf = BytesIO()
        img.save(buf, "PNG")
        data = buf.getvalue()
        width, height = 256, 256
        ext = "png"
        mimetype = "image/png"

    print(".", end="", flush=True)
    mxc = await matrix.upload(data, mimetype, f"{document.id}.{ext}")
    print(".", flush=True)
    return util.make_sticker(mxc, width, height, len(data), mimetype=mimetype), data


def add_meta(document: Document, info: matrix.StickerInfo, pack: StickerSetFull) -> None:
    for attr in document.attributes:
        if isinstance(attr, DocumentAttributeSticker):
            info["body"] = attr.alt
    info["id"] = f"tg-{document.id}"
    info["net.maunium.telegram.sticker"] = {
        "pack": {
            "id": str(pack.set.id),
            "short_name": pack.set.short_name,
        },
        "id": str(document.id),
        "emoticons": [],
    }


async def reupload_pack(client: TelegramClient, pack: StickerSetFull, output_dir: str, limit: int = 0) -> None:
    pack_path = os.path.join(output_dir, f"{pack.set.short_name}.json")
    try:
        os.mkdir(os.path.dirname(pack_path))
    except FileExistsError:
        pass

    count = pack.set.count if hasattr(pack.set, 'count') else len(pack.documents)
    print(f"Reuploading {pack.set.title} with {count} stickers "
          f"and writing output to {pack_path}")

    already_uploaded = {}
    try:
        with util.open_utf8(pack_path) as pack_file:
            existing_pack = json.load(pack_file)
            already_uploaded = {int(sticker["net.maunium.telegram.sticker"]["id"]): sticker
                                for sticker in existing_pack["stickers"]}
            print(f"Found {len(already_uploaded)} already reuploaded stickers")
    except FileNotFoundError:
        pass

    stickers_data: Dict[str, bytes] = {}
    reuploaded_documents: Dict[int, matrix.StickerInfo] = {}
    documents = pack.documents[:limit] if limit > 0 else pack.documents
    for document in documents:
        try:
            reuploaded_documents[document.id] = already_uploaded[document.id]
            print(f"Skipped reuploading {document.id}")
        except KeyError:
            result = await reupload_document(client, document)
            if result is None:
                print(f"Skipped unsupported format {document.id}")
                continue
            reuploaded_documents[document.id], data = result
            stickers_data[reuploaded_documents[document.id]["url"]] = data  # TODO: change
        # Always ensure the body and telegram metadata is correct
        add_meta(document, reuploaded_documents[document.id], pack)

    for sticker in pack.packs:
        if not sticker.emoticon:
            continue
        for document_id in sticker.documents:
            if document_id not in reuploaded_documents:
                print(f"Warning: document {document_id} not in reuploaded documents, skipping")
                continue
            doc = reuploaded_documents[document_id]
            if doc["body"] == "":
                doc["body"] = sticker.emoticon
            if "emoticons" not in doc.get("net.maunium.telegram.sticker", {}):
                doc["net.maunium.telegram.sticker"]["emoticons"] = []
            doc["net.maunium.telegram.sticker"]["emoticons"].append(sticker.emoticon)

    with util.open_utf8(pack_path, "w") as pack_file:
        json.dump({
            "title": pack.set.title,
            "id": f"tg-{pack.set.id}",
            "net.maunium.telegram.pack": {
                "short_name": pack.set.short_name,
                "hash": str(pack.set.hash),
            },
            "stickers": list(reuploaded_documents.values()),
        }, pack_file, ensure_ascii=False)
    print(f"Saved {pack.set.title} as {pack.set.short_name}.json")

    util.add_thumbnails(list(reuploaded_documents.values()), stickers_data, output_dir)
    util.add_to_index(os.path.basename(pack_path), output_dir)


pack_url_regex = re.compile(r"^(?:(?:https?://)?(?:t|telegram)\.(?:me|dog)/addstickers/)?"
                            r"([A-Za-z0-9-_]+)"
                            r"(?:\.json)?$")

parser = argparse.ArgumentParser()

parser.add_argument("--list", help="List your saved sticker packs", action="store_true")
parser.add_argument("--session", help="Telethon session file name", default="sticker-import")
parser.add_argument("--file", help="Select file URLs to import", default="sticker/links.json")
parser.add_argument("--config",
                    help="Path to JSON file with Matrix homeserver and access_token",
                    type=str, default="config.json")
parser.add_argument("--output-dir", help="Directory to write packs to", default="web/packs/",
                    type=str)
parser.add_argument("--limit", help="Limit number of stickers to import per pack", type=int, default=0)


async def main(args: argparse.Namespace) -> None:
    config = await matrix.load_config(args.config)
    client = TelegramClient(args.session, 298751, "cb676d6bae20553c9996996a8f52b4d7")
    await client.start(phone=None, password=None, bot_token=config["telegram_bot_token"])

    if args.list:
        stickers: AllStickers = await client(GetAllStickersRequest(hash=0))
        index = 1
        width = len(str(len(stickers.sets)))
        print("Your saved sticker packs:")
        for saved_pack in stickers.sets:
            print(f"{index:>{width}}. {saved_pack.title} "
                  f"(t.me/addstickers/{saved_pack.short_name})")
            index += 1
    elif args.file:
        with open(args.file, "r") as f:
            pack_url_list = json.load(f)
        input_packs = []
        for pack_url in pack_url_list:
            match = pack_url_regex.match(pack_url)
            if not match:
                print(f"'{pack_url}' doesn't look like a sticker pack URL")
                return
            input_packs.append(InputStickerSetShortName(short_name=match.group(1)))
        for input_pack in input_packs:
            pack: StickerSetFull = await client(GetStickerSetRequest(input_pack, hash=0))
            await reupload_pack(client, pack, args.output_dir, args.limit)
    else:
        parser.print_help()

    await client.disconnect()


def cmd() -> None:
    asyncio.run(main(parser.parse_args()))


if __name__ == "__main__":
    cmd()
