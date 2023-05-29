import argparse
from pathlib import Path
from typing import List, Tuple
import tempfile

import pandas as pd
import re

from dataframes import compute_time_range, trim_repaired_into_interval
from dataframes import repair_dropped_frames, compute_time_step

from video import extract_frames
from video import rebuild_video
from video import extract_video_info


DEFAULT_THRESHOLD_MILLIS = 10
DEFAULT_THRESHOLD_NANOS = DEFAULT_THRESHOLD_MILLIS * 1000 * 1000  # millis * micros * nanos


def scan_session_dir(input_dir: Path) -> Tuple[List[str], List[pd.DataFrame], List[str]]:
    #
    # Find all CSV files in the directory and read it into a data frame
    # Use the following regular expression to check of the client ID is a 16-digit hexadecimal.
    clientIDpattern = "[\\da-f]" * 16
    patt = re.compile("^" + clientIDpattern + "$")

    # Fill this list with the client IDs found n the directory
    clientIDs: List[str] = []
    for p in input_dir.iterdir():
        # Check if the ClientID complies to the numerical format (using regex).
        res = patt.match(p.stem)
        if res:
            print("Found client -->", p.stem)
            clientIDs.append(p.stem)
        else:
            print("Discarding ", p.stem)

    #
    # Accumulates the list of dataframes and mp4 files in the same order of the client IDs.
    df_list: List[pd.DataFrame] = []
    mp4_list: List[str] = []

    for cID in clientIDs:
        client_dir = input_dir / cID
        CSVs = list(client_dir.glob("*.csv"))
        MP4s = list(client_dir.glob("*.mp4"))
        #
        # Consistency check. Each clientID folder must have exactly 1 CSV and 1 mp4.
        if len(CSVs) != 1:
            raise Exception(f"Expecting 1 CSV file for client {cID}. Found {len(CSVs)}.")

        if len(MP4s) != 1:
            raise Exception(f"Expecting 1 MP4 file for client {cID}. Found {len(MP4s)}.")

        csv_file = CSVs[0]
        mp4_file = MP4s[0]

        df: pd.DataFrame = pd.read_csv(csv_file, header=None)

        df_list.append(df)
        mp4_list.append(str(mp4_file))

    return clientIDs, df_list, mp4_list


#
#
#
def main(input_dir: Path, output_dir: Path, threshold_ns: int):

    print(f"Scanning dir {str(input_dir)}...")
    clientIDs, df_list, mp4_list = scan_session_dir(input_dir)

    n_clients = len(clientIDs)

    #
    # Print collected info
    for i in range(n_clients):
        cID = clientIDs[i]
        df = df_list[i]
        mp4 = mp4_list[i]
        print(f"For client ID {cID}: {len(df)} frames for file {mp4}")

    #
    # Repair CSVs
    repaired_df_list: List[pd.DataFrame] = []
    for cID, df in zip(clientIDs, df_list):
        time_step = compute_time_step(df)
        repaired_df = repair_dropped_frames(df=df, time_step=time_step)
        repaired_df_list.append(repaired_df)

    assert len(clientIDs) == len(df_list) == len(mp4_list) == len(repaired_df_list)

    #
    # Trim CSVs
    # Find time ranges
    min_common, max_common = compute_time_range(repaired_df_list)
    # Trim the data frames to the time range
    trimmed_dataframes = trim_repaired_into_interval(repaired_df_list, min_common, max_common, threshold_ns)

    assert len(clientIDs) == len(trimmed_dataframes), f"Expected {len(clientIDs)} trimmed dataframes. Found f{len(trimmed_dataframes)}"

    # Check that all the resulting dataframes have the same number of rows
    print("Checking if all clients we have the same number of frames in the repaired amd trimmed tables...")
    client0ID = clientIDs[0]
    client0size = len(trimmed_dataframes[0])
    print(f"Client {client0ID} has {client0size} frames.")
    for cID, df in zip(clientIDs[1:], trimmed_dataframes[1:]):
        dfsize = len(df)
        if client0size != dfsize:
            raise Exception(f"For client {cID}: expecting {client0size} frames. Found {dfsize}."
                            f" This might be due to an excessive phase offset during recording."
                            f" Try to increase the threshold.")

    print("Good. All trimmed dataframes have the same number of entries.")

    #
    # Unpack the original videos, and repack them according to repaired and trimmed dataframes.
    for i, cID in enumerate(clientIDs):
        orig_df = df_list[i]
        trimmed_df = trimmed_dataframes[i]
        video_file = mp4_list[i]
        # Create a temporary directory for frames unpacking
        with tempfile.TemporaryDirectory(prefix="RecSyncNG", suffix=cID) as tmp_dir:
            # Extract the frames from the original videos
            # and rename the file names to the timestamps
            print(f"Extracting {len(orig_df)} frames from '{video_file}'...")
            extract_frames(video_file=video_file, timestamps_df=orig_df, output_dir=tmp_dir)

            # Reconstruct videos
            vinfo = extract_video_info(video_path=video_file)
            video_out_filepath = output_dir / (cID + ".mp4")
            rebuild_video(dir=Path(tmp_dir), frames=trimmed_df, video_info=vinfo, outfile=video_out_filepath)
            # And save also the CSV
            csv_out_filepath = video_out_filepath.with_suffix(".csv")
            trimmed_df.to_csv(path_or_buf=csv_out_filepath, header=True, index=False)


#
# MAIN
if __name__ == "__main__":

    parser = argparse.ArgumentParser(
        description="Fixes the videos produced by the RecSync recording sessions."
                    "Output videos will have the same number of frames,"
                    "with missing/dropped frames inserted as (black) artificial data."
    )
    parser.add_argument(
        "--infolder", "-i", type=str, help="The folder containing the collected videos and CSV files with the timestamps.",
        required=True
    )
    parser.add_argument(
        "--outfolder", "-o", type=str, help="The folder where the repaired and aligned frames will be stored.",
        required=True
    )
    parser.add_argument(
        "--threshold", "-t", type=int, help="The allowed difference in ms between corresponding frames on different videos."
                                            " Increase this is post processing fails with trimmed tables of different sizes."
                                            f" Default is now {DEFAULT_THRESHOLD_MILLIS} ms.",
        required=False,
        default=DEFAULT_THRESHOLD_MILLIS
    )

    args = parser.parse_args()

    infolder = Path(args.infolder)
    outfolder = Path(args.outfolder)
    threshold_millis = args.threshold
    threshold_nanos = threshold_millis * 1000 * 1000

    if not infolder.exists():
        raise Exception(f"Input folder '{infolder}' doesn't exist.")

    if not outfolder.exists():
        raise Exception(f"Output folder '{outfolder}' doesn't exist.")

    main(infolder, outfolder, threshold_nanos)
