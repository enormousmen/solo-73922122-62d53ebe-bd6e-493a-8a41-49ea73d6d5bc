# disk1 notes

- This image is never mounted by the API.
- e2fsck and resize2fs operate directly on the image file.
- The checksum of this file is verified after each expansion.
