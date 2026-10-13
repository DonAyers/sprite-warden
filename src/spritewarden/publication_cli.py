"""No-network CLI for explicit publication authorization and readback receipts."""
import argparse
import json
from . import publication


def main(argv=None):
    p=argparse.ArgumentParser(prog='warden publish')
    p.add_argument('--project',default='.')
    sub=p.add_subparsers(dest='command',required=True)
    prepare=sub.add_parser('prepare',help='Authorize an exact native MCP rotation replacement; does not execute it')
    for name in ('source','image','target','by','reason'):
        prepare.add_argument('--'+name,required=True)
    upload=prepare.add_mutually_exclusive_group(required=True)
    upload.add_argument('--uploaded-file',help='Provider-downloaded upload PNG matching the local source')
    upload.add_argument('--no-upload-readback',metavar='REASON',help='Provider and reason upload download is unavailable; post-save confirmation remains mandatory')
    confirm=sub.add_parser('confirm',help='Verify operator-downloaded remote readback after native save')
    confirm.add_argument('receipt_id');confirm.add_argument('--readback',required=True)
    review=sub.add_parser('review',help='Create a neutral-background contact sheet and full findings JSON, no model calls')
    review.add_argument('--views',required=True);review.add_argument('--out',required=True)
    verify=sub.add_parser('verify-character',help='Compare a fresh complete native character download with reviewed local rotations')
    verify.add_argument('character_id');verify.add_argument('--download-dir',required=True)
    cancel=sub.add_parser('cancel',help='Revoke unused/uncertain local authorization; does not undo remote changes')
    cancel.add_argument('receipt_id');cancel.add_argument('--by',required=True);cancel.add_argument('--reason',required=True)
    args=p.parse_args(argv)
    try:
        if args.command=='review':
            from .review_board import build
            board,report=build(publication.within(args.project,args.views))
            output=publication.within(args.project,args.out)
            report_path=output.with_suffix(output.suffix+'.json')
            if output.exists() or report_path.exists():raise ValueError('review outputs already exist; choose a new name')
            output.parent.mkdir(parents=True,exist_ok=True)
            with output.open('xb') as stream:board.save(stream,'PNG')
            with report_path.open('x',encoding='utf-8') as stream:json.dump(report,stream,indent=2)
            result={'board':str(output),'report':str(report_path),'views':len(report),'model_calls':0}
        elif args.command=='prepare':
            result=publication.prepare(args.project,args.source,args.image,args.target,uploaded_file=args.uploaded_file,
                                       provider_gap=args.no_upload_readback,actor=args.by,reason=args.reason)
        elif args.command=='verify-character':
            result=publication.verify_character(args.project,args.character_id,args.download_dir)
        elif args.command=='cancel':
            result=publication.cancel(args.project,args.receipt_id,actor=args.by,reason=args.reason)
        else:
            result=publication.confirm(args.project,args.receipt_id,args.readback)
        print(json.dumps(result,indent=2));return 0
    except (ValueError,OSError,RuntimeError,KeyError) as exc:
        print(json.dumps({'error':str(exc),'type':type(exc).__name__}));return 2
