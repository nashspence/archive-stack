from __future__ import annotations

import argparse


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mount_point")
    parser.add_argument("device_id")
    args = parser.parse_args()
    print(f"archive {args.device_id} from {args.mount_point}")


if __name__ == "__main__":
    main()
