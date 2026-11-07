# Generate or edit a PNG

Generate an image:

```sh
python -m examples.codex.hosted_tools.image_generation.main
```

The script writes `generated.png`. Set `HOSTED_IMAGE_OUTPUT` to choose a different
path. The script overwrites that path if it exists. Your application owns the
output path and any storage or sharing of the image bytes.

To edit an image, supply a local PNG and a PNG alpha mask of the same dimensions.
Transparent mask areas identify the area to edit. The script reads both files
and sends inline base64 data; local paths do not go to the provider.

```sh
export HOSTED_IMAGE_INPUT=/absolute/path/input.png
export HOSTED_IMAGE_MASK=/absolute/path/mask.png
export HOSTED_IMAGE_OUTPUT=/absolute/path/edited.png
python -m examples.codex.hosted_tools.image_generation.edit
```

The edit requests one partial image. The provider can return fewer previews.
Partial-image events carry `partial_image_b64` and `partial_image_index`;
they do not replace the final result. These scripts save only the completed
`image_generation_call.result`. The final value contains base64 image bytes,
not a file path or a download URL.

Both scripts use `gpt-5.2` as the outer Responses model. The provider chooses
its default image generator. Image options depend on that generator and account
access. A forced edit needs image input. Transparent output requires a format
such as PNG or WebP. Do not assume that every model accepts every option.

OpenAI owns generation. Your application owns input files, output files, and
any uploaded resources you create separately. Session close and cancellation
do not delete provider resources. See the shared [setup notes](../README.md)
and the provider's [image guide](https://developers.openai.com/api/docs/guides/image-generation).
