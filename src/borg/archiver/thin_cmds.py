import argparse
import logging
import os
import time

from ..archive import Archive, ThinObjectProcessors, ChunksProcessor
from ..archive import BackupError, BackupOSError
from ..compress import CompressionSpec
from ..constants import *  # NOQA
from ..helpers import archivename_validator, comment_validator, lv_validator
from ..helpers import timestamp, archive_ts_now
from ..helpers import basic_json_data, json_print
from ..helpers import log_multi
from ..helpers import sig_int
from ..manifest import Manifest

from ._common import with_repository, Highlander

from ..logger import create_logger

logger = create_logger(__name__)

class ThinMixIn:
    @with_repository(cache=True, exclusive=True, compatibility=(Manifest.Operation.READ, Manifest.Operation.WRITE))
    def do_create_thin(self, args, repository, manifest, cache):
        """Create a backup archive from LVM thin volume(s)"""
        self.output_filter = args.output_filter
        self.output_list = args.output_list

        t0 = archive_ts_now()
        t0_monotonic = time.monotonic()
        logger.info('Creating archive at "%s"' % args.location.processed)

        archive = Archive(
            manifest,
            args.name,
            cache=cache,
            create=True,
            checkpoint_interval=args.checkpoint_interval,
            progress=args.progress,
            chunker_params=('thinlv',), # TODO: is this legal? the chunk size might vary between thin pools...
            start=t0,
            start_monotonic=t0_monotonic,
            log_json=args.log_json,
        )
        cp = ChunksProcessor(
            cache=cache,
            key=manifest.key,
            add_item=archive.add_item,
            write_checkpoint=archive.write_checkpoint,
            checkpoint_interval=args.checkpoint_interval,
            rechunkify=False,
        )
        top = ThinObjectProcessors(
            archive=archive,
            cache=cache,
            add_item=archive.add_item,
            process_file_chunks=cp.process_file_chunks,
            show_progress=args.progress,
            log_json=args.log_json,
            iec=args.iec,
            file_status_printer=self.print_file_status,
        )

        for vg, lv in args.lvs:
            try:
                status = top.process_lv(vg=vg, lv=lv)
            except (BackupOSError, BackupError) as e:
                self.print_warning('%s/%s: %s', vg, lv, e)
                status = 'E'
            self.print_file_status(f'{vg}/{lv}', status)
            if status is not None:
                top.stats.files_stats[status] += 1

        if args.progress:
            archive.stats.show_progress(final=True)
        archive.stats += top.stats
        archive.stats.rx_bytes = getattr(repository, "rx_bytes", 0)
        archive.stats.tx_bytes = getattr(repository, "tx_bytes", 0)
        if sig_int:
            self.print_error("Got Ctrl-C / SIGINT.")
        else:
            archive.save(comment=args.comment, timestamp=args.timestamp)
            args.stats |= args.json
            if args.stats:
                if args.json:
                    json_print(basic_json_data(archive.manifest, cache=archive.cache, extra={"archive": archive}))
                else:
                    log_multi(str(archive), str(archive.stats), logger=logging.getLogger("borg.output.stats"))

        return self.exit_code

    def build_parser_thin(self, subparsers, common_parser, mid_common_parser):
        from ._common import process_epilog
        create_thin_epilog = process_epilog(
            """
        This command creates a backup archive from LVM thin volumes.

        """
        )
        subparser = subparsers.add_parser(
            "tcreate",
            parents=[common_parser],
            add_help=False,
            description=self.do_create_thin.__doc__,
            epilog=create_thin_epilog,
            formatter_class=argparse.RawDescriptionHelpFormatter,
            help=self.do_create_thin.__doc__,
        )
        subparser.set_defaults(func=self.do_create_thin)
        subparser.add_argument(
            "-s",
            "--stats",
            dest="stats",
            action="store_true",
            default=False,
            help="print statistics for the created archive",
        )
        subparser.add_argument(
            "--list",
            dest="output_list",
            action="store_true",
            default=False,
            help="output verbose list of items (files, dirs, ...)",
        )
        subparser.add_argument(
            "--filter",
            dest="output_filter",
            metavar="STATUSCHARS",
            action=Highlander,
            help="only display items with the given status characters",
        )
        subparser.add_argument("--json", action="store_true", help="output stats as JSON (implies --stats)")

        archive_group = subparser.add_argument_group("Archive options")
        archive_group.add_argument(
            "--comment",
            metavar="COMMENT",
            dest="comment",
            type=comment_validator,
            default="",
            help="add a comment text to the archive",
        )
        archive_group.add_argument(
            "--timestamp",
            dest="timestamp",
            type=timestamp,
            default=None,
            metavar="TIMESTAMP",
            help="manually specify the archive creation date/time (yyyy-mm-ddThh:mm:ss[(+|-)HH:MM] format, "
            "(+|-)HH:MM is the UTC offset, default: local time zone). Alternatively, give a reference file/directory.",
        )
        archive_group.add_argument(
            "-c",
            "--checkpoint-interval",
            dest="checkpoint_interval",
            type=int,
            default=1800,
            metavar="SECONDS",
            help="write checkpoint every SECONDS seconds (Default: 1800)",
        )
        archive_group.add_argument(
            "-C",
            "--compression",
            metavar="COMPRESSION",
            dest="compression",
            type=CompressionSpec,
            default=CompressionSpec("lz4"),
            help="select compression algorithm, see the output of the " '"borg help compression" command for details.',
        )

        subparser.add_argument("name", metavar="NAME", type=archivename_validator, help="specify the archive name")
        subparser.add_argument("lvs", metavar="LV", nargs="*", type=lv_validator, action="extend", help="LVs to backup (`vg/lv`)")
